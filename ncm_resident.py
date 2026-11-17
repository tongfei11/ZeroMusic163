"""Serialize requests through one warm, isolated official CLI process."""

import json
import os
from pathlib import Path
import selectors
import subprocess
from time import monotonic


class Resident:
    def __init__(self, executable, environment, home):
        self.home = home
        self.pending = b""
        script = Path(__file__).with_name("ncm-resident.cjs")
        self.process = subprocess.Popen(
            ["nice", "-n", "10", "node", str(script), str(Path(executable).resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=environment, cwd=home,
        )

    def request(self, request):
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()
        deadline = monotonic() + 30
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired("resident", 30)
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk or len(self.pending) + len(chunk) > 2 * 1024 * 1024:
                    raise OSError("Official CLI service disconnected")
                self.pending += chunk
        frame, self.pending = self.pending.split(b"\n", 1)
        response = json.loads(frame)
        return response["output"], response["errors"], response["code"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
