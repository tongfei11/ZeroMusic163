#!/usr/bin/env python3
"""Start the ZeroMusic163 application from Cardputer Zero's launcher."""

import argparse
import fcntl
from pathlib import Path
import signal
import sys

from app import App
from device import Device, FRAMEBUFFER, KEYBOARD, render
from netease import Netease


INSTALL_ROOT = Path.home() / ".local/share/zero-music163"
VERSION = Path(__file__).with_name("VERSION").read_text().strip()


def main():
    parser = argparse.ArgumentParser(description="ZeroMusic163 for Cardputer Zero")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--framebuffer", default=FRAMEBUFFER)
    parser.add_argument("--keyboard", default=KEYBOARD)
    parser.add_argument("--profile", type=Path, default=INSTALL_ROOT / "profile")
    parser.add_argument("--cli", default=str(INSTALL_ROOT / "runtime/node_modules/.bin/ncm-cli"))
    args = parser.parse_args()
    try:
        args.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (args.profile / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            return run(args)
    except OSError:
        print("无法打开 ZeroMusic163 的设备或配置目录。", file=sys.stderr)
        return 1


def run(args):
    device = Device(args.framebuffer, args.keyboard)
    try:
        app = App(Netease(args.profile, args.cli))
        def stop(_signal, _frame):
            app.running = False

        previous_handler = signal.signal(signal.SIGTERM, stop)
        try:
            last = None
            while app.running:
                app.tick()
                if not app.running:
                    break
                view = app.view()
                if view != last:
                    device.show(render(view))
                    last = view
                for key, value in device.events(0.12):
                    app.key(key, value)
                    if not app.running:
                        break
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            app.close()
    finally:
        device.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
