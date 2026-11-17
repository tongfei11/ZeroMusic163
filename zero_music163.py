#!/usr/bin/env python3
"""Start the ZeroMusic163 application from Cardputer Zero's launcher."""

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep

from app import App
from device import Device, FRAMEBUFFER, KEYBOARD, render, row_scroll_frame, title_scroll_frame
from netease import Netease


INSTALL_ROOT = Path.home() / ".local/share/zero-music163"
APP_ROOT = Path(__file__).resolve().parent
PACKAGED_CLI = APP_ROOT / "node_modules/.bin/ncm-cli"
VERSION = (APP_ROOT / "VERSION").read_text().strip()


def try_lock(lock):
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def start_background(args, lock):
    reader, writer = os.pipe()
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--profile", str(args.profile),
             "--cli", args.cli, "--background", str(lock.fileno()), str(reader)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(lock.fileno(), reader),
        )
    except OSError:
        os.close(writer)
        raise
    finally:
        os.close(reader)
    return writer


def main():
    parser = argparse.ArgumentParser(description="ZeroMusic163 for Cardputer Zero")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--framebuffer", default=FRAMEBUFFER)
    parser.add_argument("--keyboard", default=KEYBOARD)
    parser.add_argument("--profile", type=Path, default=INSTALL_ROOT / "profile")
    parser.add_argument("--cli", default=str(PACKAGED_CLI if PACKAGED_CLI.is_file()
                                             else INSTALL_ROOT / "runtime/node_modules/.bin/ncm-cli"))
    parser.add_argument("--background", nargs=2, type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    handoff = None
    try:
        args.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        if args.background:
            lock_fd, ready_fd = args.background
            with os.fdopen(lock_fd, "ab"), os.fdopen(ready_fd, "rb") as ready:
                # The old UI closes the pipe after releasing the display and UI lock.
                ready.read(1)
                run_background(args)
            return 0
        with (args.profile / ".ui-lock").open("a") as ui_lock:
            if not try_lock(ui_lock):
                return 0
            with (args.profile / ".lock").open("a") as lock:
                deadline = monotonic() + 5
                while not try_lock(lock):
                    if monotonic() >= deadline:
                        print("后台播放正在交接，请稍后重新打开。", file=sys.stderr)
                        return 1
                    sleep(0.05)
                if run(args):
                    handoff = start_background(args, lock)
        return 0
    except OSError:
        print("无法打开 ZeroMusic163 的设备或配置目录。", file=sys.stderr)
        return 1
    finally:
        if handoff is not None:
            os.close(handoff)


def run_background(args):
    with (args.profile / ".ui-lock").open("a") as ui_lock:
        # A UI that launched during the handoff already owns the next session.
        if not try_lock(ui_lock):
            return
        fcntl.flock(ui_lock, fcntl.LOCK_UN)
        app = App(Netease(args.profile, args.cli), settings_path=args.profile / "settings.json",
                  background=True)

        def stop(_signal, _frame):
            app.running = False

        previous_handler = signal.signal(signal.SIGTERM, stop)
        takeover = False
        try:
            while app.running:
                if not try_lock(ui_lock):
                    takeover = True
                    break
                fcntl.flock(ui_lock, fcntl.LOCK_UN)
                app.tick()
                if getattr(app, "background_finished", False):
                    break
                sleep(0.12)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            app.close(keep_playing=takeover)


def run(args):
    device = Device(args.framebuffer, args.keyboard)
    try:
        app = App(Netease(args.profile, args.cli), settings_path=args.profile / "settings.json")
        def stop(_signal, _frame):
            app.running = False

        previous_handler = signal.signal(signal.SIGTERM, stop)
        try:
            last = None
            last_offset = 0
            last_row_offset = 0
            while app.running:
                frame_start = monotonic()
                app.tick()
                if not app.running:
                    break
                view = app.view()
                elapsed = view.pop("title_scroll", None)
                row_elapsed = view.pop("row_scroll_elapsed", None)
                exit_prompt = "exit_progress" in view
                toast_visible = exit_prompt or bool(view.get("notice"))
                offset, interval = 0, 0.12
                if exit_prompt:
                    interval = 0.05
                elif elapsed is not None and not toast_visible:
                    offset, interval = title_scroll_frame(view["heading"], elapsed)
                row_offset = 0
                if row_elapsed is not None and not toast_visible:
                    row_offset, row_interval = row_scroll_frame(view, row_elapsed)
                    interval = min(interval, row_interval)
                if view != last:
                    frame = view | {"title_scroll": offset}
                    if row_elapsed is not None:
                        frame["row_scroll"] = row_offset
                    device.show(render(frame))
                    last = view
                elif offset != last_offset:
                    device.show_title(view["heading"], offset)
                elif row_offset != last_row_offset:
                    device.show_row_title(view, row_offset)
                last_offset = offset
                last_row_offset = row_offset
                for key, value in device.events(max(0, frame_start + interval - monotonic())):
                    app.key(key, value)
                    if not app.running:
                        break
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            keep_playing = app.background_playback and app.should_keep_playing
            app.close(keep_playing=keep_playing)
    finally:
        device.close()
    return keep_playing


if __name__ == "__main__":
    raise SystemExit(main())
