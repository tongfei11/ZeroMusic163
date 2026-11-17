"""Remove the installed application and its current user's local data."""

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
from time import monotonic, sleep

from netease import Netease


def lock_available(handle):
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def main():
    if os.getuid() == 0:
        print("请用运行 ZeroMusic163 的普通用户执行卸载。")
        return 1
    root = Path.home() / ".local/share/zero-music163"
    profile = root / "profile"
    if root.is_symlink() or profile.is_symlink():
        print("应用数据目录是符号链接，请先核对真实位置后卸载。")
        return 1
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (profile / ".ui-lock").open("a") as ui, (profile / ".lock").open("a") as session:
        if not lock_available(ui):
            print("请先退出 ZeroMusic163 界面，再执行卸载。")
            return 1
        deadline = monotonic() + 8
        while not lock_available(session):
            if monotonic() >= deadline:
                print("后台播放器正在退出，请稍后重试卸载。")
                return 1
            sleep(0.1)
        client = Netease(profile, "")
        client.adopt_playback()
        client.reset()
        if Path(__file__).resolve().parent == Path("/usr/lib/zero-music163"):
            subprocess.run(["sudo", "dpkg", "--remove", "zero-music163"], check=True)
        else:
            subprocess.run([
                "sudo", "rm", "-rf", "--", "/usr/local/lib/zero-music163",
                "/usr/share/APPLaunch/applications/zero-music163.desktop",
                "/usr/share/APPLaunch/share/images/zero-music163.png",
            ], check=True)
        shutil.rmtree(root)
    print("已卸载 ZeroMusic163，并清除开发者配置、音乐登录和本地数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
