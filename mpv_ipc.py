"""Keep a single native audio process alive between tracks."""

import json
import socket
import subprocess
import time


class MpvError(Exception):
    pass


class Mpv:
    def __init__(self, address, closing):
        self.address = address
        self.closing = closing
        self.process = None

    @staticmethod
    def send(client, command, request_id):
        frame = {"command": list(command), "request_id": request_id}
        client.sendall((json.dumps(frame) + "\n").encode())

    def requests(self, *commands):
        result = {}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.address))
                for index, command in enumerate(commands):
                    self.send(client, command, index)
                deadline = time.monotonic() + 1.5
                with client.makefile("rb") as reader:
                    while len(result) < len(commands) and time.monotonic() < deadline:
                        line = reader.readline(65536)
                        if not line:
                            break
                        response = json.loads(line)
                        index = response.get("request_id")
                        if isinstance(index, int) and 0 <= index < len(commands):
                            result[index] = response
        except (OSError, ValueError, AttributeError):
            pass
        return [result.get(index, {}) for index in range(len(commands))]

    def properties(self, *names):
        replies = self.requests(*[("get_property", name) for name in names])
        return {name: response.get("data") for name, response in zip(names, replies)}

    def command(self, *command):
        if self.requests(command)[0].get("error") != "success":
            raise MpvError("播放器未响应。")

    def ready(self):
        return isinstance(self.properties("idle-active")["idle-active"], bool)

    def wait_until(self, ready, seconds, error):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.closing():
            if ready():
                return
            if self.process is not None and self.process.poll() is not None:
                break
            time.sleep(0.15)
        raise MpvError(error)

    def start(self):
        if self.ready():
            return
        if self.process is None or self.process.poll() is not None:
            try:
                self.process = subprocess.Popen(
                    ["mpv", "--no-config", "--idle=yes", "--no-video", "--really-quiet",
                     "--force-window=no", f"--input-ipc-server={self.address}"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                raise MpvError("无法启动设备播放器。") from None
        self.wait_until(self.ready, 12, "设备播放器启动超时。")

    def play(self, url, volume):
        self.start()
        self.command("set_property", "volume", volume)
        previous = self.properties("playlist/0/id")["playlist/0/id"]
        try:
            if self.load_file(url):
                return
        except (OSError, ValueError, AttributeError):
            pass

        # An IPC disconnect can lose the event even when mpv loaded the song.
        state = self.properties("playlist/0/id", "idle-active", "time-pos")
        if (state["idle-active"] is False and state["time-pos"] is not None
                and state["playlist/0/id"] is not None and state["playlist/0/id"] != previous):
            return
        raise MpvError("音频载入失败或超时，请重试。")

    def load_file(self, url):
        deadline = time.monotonic() + 18
        entry_id = None
        accepted = loaded = False
        pending = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(self.address))
            self.send(client, ("loadfile", url, "replace"), 1)
            self.send(client, ("set_property", "pause", False), 2)
            while time.monotonic() < deadline and not self.closing():
                try:
                    chunk = client.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                pending += chunk
                if len(pending) > 65536:
                    break
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    event = json.loads(line)
                    event_type = event.get("event")
                    if event.get("request_id") == 1:
                        if event.get("error") != "success":
                            raise MpvError("播放器无法打开这首歌曲。")
                        accepted = True
                    elif event_type == "start-file":
                        entry_id = event.get("playlist_entry_id")
                        loaded = False
                    elif event_type == "file-loaded" and entry_id is not None:
                        loaded = True
                    elif (event_type == "end-file" and entry_id is not None
                          and event.get("playlist_entry_id") == entry_id):
                        if event.get("reason") == "error":
                            raise MpvError("播放器无法解码这首歌曲。")
                        entry_id = None
                        loaded = False
                    if accepted and loaded:
                        return True
        return False

    def state(self):
        state = self.properties("idle-active", "pause", "time-pos", "duration", "volume")
        if state["idle-active"] is True:
            status = "stopped"
        elif state["idle-active"] is False and state["pause"] is not None:
            status = "paused" if state["pause"] else "playing"
        else:
            status = "unknown"
        return {"status": status, "position": state["time-pos"], "duration": state["duration"],
                "volume": state["volume"]}

    def close(self):
        self.requests(("quit",))
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
