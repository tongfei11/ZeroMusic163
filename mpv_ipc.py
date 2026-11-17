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

    def requests(self, *commands):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.address))
                for index, command in enumerate(commands):
                    frame = {"command": list(command), "request_id": index}
                    client.sendall((json.dumps(frame) + "\n").encode())
                result = {}
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
                return [result.get(index, {}) for index in range(len(commands))]
        except (OSError, ValueError, AttributeError):
            return [{} for _ in commands]

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
        self.command("loadfile", url, "replace")
        self.command("set_property", "pause", False)

        def loaded():
            state = self.properties("path", "time-pos", "audio-out-params")
            return (state["path"] == url and isinstance(state["time-pos"], (int, float))
                    and isinstance(state["audio-out-params"], dict))

        self.wait_until(loaded, 18, "音频载入失败或超时，请重试。")

    def state(self):
        state = self.properties("idle-active", "pause", "time-pos", "duration", "volume")
        active = state["idle-active"] is False and state["time-pos"] is not None
        return {"status": ("paused" if state["pause"] else "playing") if active else "stopped",
                "position": state["time-pos"], "duration": state["duration"], "volume": state["volume"]}

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
