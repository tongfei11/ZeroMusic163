"""Adapter for the official @music163/ncm-cli 0.1.7."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from threading import Lock, RLock
import time
from urllib.parse import urlsplit

from mpv_ipc import Mpv, MpvError


PLAYLIST_PAGE_SIZE = 30


class NeteaseError(Exception):
    """A user-readable error which never includes raw CLI output."""

    def __init__(self, message, *, reauth=False, check_session=False):
        super().__init__(message)
        self.reauth = reauth
        self.check_session = check_session


class Netease:
    def __init__(self, profile: Path, executable: str):
        self.profile = profile.expanduser().resolve()
        self.executable = executable
        self._lock = RLock()
        self._process_lock = Lock()
        self._process = None
        self._closing = False
        self._pending_login = None
        self._current = self.profile / "current"
        self._player = None
        self._media_urls = OrderedDict()
        self._media_lock = Lock()

    def configured(self) -> bool:
        home = self._current
        return (home / ".config/ncm-cli/credentials.enc.json").is_file()

    @staticmethod
    def _runtime_path(home):
        tag = hashlib.sha256(os.fsencode(home)).hexdigest()[:16]
        return Path(f"/tmp/zm-{os.getuid()}-{tag}")

    def _runtime(self, home):
        path = self._runtime_path(home)
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NeteaseError("无法创建隔离的播放器目录。")
        alias = path / "home"
        # CLI places its Unix sockets under homedir. Keep the socket path short.
        try:
            alias.symlink_to(home, target_is_directory=True)
        except FileExistsError:
            if not alias.is_symlink() or alias.readlink() != home:
                raise NeteaseError("无法创建隔离的播放器目录。") from None
        return path

    def _run(self, *args, home=None, private_input=None, allow_failure=False, allow_empty=False, media_file=None):
        with self._lock:
            if home is None:
                if not self.configured():
                    raise NeteaseError("请先配置网易云音乐 App ID 和 Private Key。")
                home = self._current.resolve(strict=True)
            try:
                environment = os.environ.copy()
                runtime = self._runtime(home)
                preload = Path(__file__).with_name("ncm-isolate.cjs").resolve()
                environment.update({
                    "NODE_OPTIONS": f"--require {json.dumps(str(preload), ensure_ascii=False)}",
                    "ZEROMUSIC_NCM_PROFILE": str(runtime / "home"),
                    "ZEROMUSIC_NCM_RUNTIME": str(runtime),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "TMPDIR": str(runtime),
                })
                environment.pop("ZEROMUSIC_NCM_CONFIG_INPUT", None)
                if private_input is not None:
                    environment["ZEROMUSIC_NCM_CONFIG_INPUT"] = "1"
                if media_file is not None:
                    environment["ZEROMUSIC_NCM_MEDIA_FILE"] = str(media_file)
                output, errors, returncode = self._invoke(args, environment, home, private_input)
            except FileNotFoundError:
                raise NeteaseError("找不到官方 ncm-cli，请检查安装。") from None
            except subprocess.TimeoutExpired:
                raise NeteaseError("网易云音乐请求超时，请重试。") from None
            except (OSError, ValueError):
                raise NeteaseError("无法启动网易云音乐 CLI。") from None

            output = output.strip()
            try:
                payload = json.loads(output)
            except ValueError:
                payload = None
            if returncode or (payload is None and not allow_empty):
                raise self._error(payload, errors + output)
            if isinstance(payload, dict) and payload.get("success") is False and not allow_failure:
                raise self._error(payload)
            return payload

    def _invoke(self, args, environment, home, private_input):
        with self._process_lock:
            if self._closing and args[0] != "stop":
                raise NeteaseError("播放器正在退出。")
            process = subprocess.Popen(
                [self.executable, *map(str, args)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=environment,
                cwd=home,
            )
            self._process = process
        try:
            with process:
                try:
                    output, errors = process.communicate(private_input or "", timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
            return output, errors, process.returncode
        finally:
            with self._process_lock:
                self._process = None

    def cancel_pending(self):
        with self._process_lock:
            self._closing = True
            if self._process and self._process.poll() is None:
                self._process.kill()

    @staticmethod
    def _error(payload, stderr=""):
        message = str(payload.get("message", "")) if isinstance(payload, dict) else stderr
        auth_terms = ("未登录", "请先登录", "未授权", "登录已过期", "登录失效", "重新登录", "token expired")
        if any(term in message.lower() for term in auth_terms):
            return NeteaseError("请先扫码登录网易云音乐。", reauth=True)
        for terms, user_message in (
            (("mpv not found", "未安装 mpv", "找不到 mpv", "mpv 未安装"), "找不到 mpv，请检查安装。"),
            (("请求总量超限", "限流"), "网易云音乐请求次数已达上限。"),
            (("fetch failed", "ECONNRESET", "ENOTFOUND", "HTTP 502", "HTTP 503", "HTTP 504"),
             "连接网易云音乐失败，按 Enter 重试。"),
            (("匿名登录失败", "签名失败", "凭证无效"), "网易云音乐未能验证应用凭据，请检查 App ID、Private Key 和网络。"),
        ):
            if any(term in message for term in terms):
                return NeteaseError(user_message)
        missing_command = "unknown command" in message.lower() or "Usage: ncm-cli [options] [command]" in message
        return NeteaseError(
            "网易云音乐命令执行失败，请检查登录状态和网络后重试。",
            check_session=missing_command,
        )

    @staticmethod
    def _qr(payload):
        if isinstance(payload, dict):
            for container in (payload, payload.get("data")):
                if isinstance(container, dict):
                    url = container.get("qrCodeUrl")
                    if isinstance(url, str):
                        try:
                            target = urlsplit(url)
                        except ValueError:
                            continue
                        if (
                            target.scheme == "https"
                            and target.username is None
                            and target.hostname
                            and (
                                target.hostname in ("music.163.com", "163cn.tv")
                                or target.hostname.endswith(".music.163.com")
                            )
                        ):
                            return url
        raise NeteaseError("网易云音乐未返回有效的扫码登录链接。")

    def configure(self, app_id, key, *, is_active=lambda: True):
        app_id, key = app_id.strip(), key.strip()
        if not app_id or not key or "\0" in app_id or "\0" in key:
            raise NeteaseError("请输入有效的 App ID 和 Private Key。")
        with self._lock:
            self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix="account-", dir=self.profile))
            link = stage.with_suffix(".link")
            committed = False
            try:
                for field, value in (("appId", app_id), ("privateKey", key), ("player", "mpv")):
                    self._run("config", "set", field, private_input=value, home=stage, allow_empty=True)
                qr = self._qr(self._run("login", "--background", home=stage))
                if not is_active():
                    raise NeteaseError("配置页面已关闭。")
                previous = self._current.resolve() if self.configured() else None
                link.symlink_to(stage.name)
                os.replace(link, self._current)
                committed = True
                self._pending_login = (time.monotonic(), qr)
                with self._media_lock:
                    self._media_urls.clear()
                if self._player is not None:
                    self._player.close()
                    self._player = None
                if previous and previous.parent == self.profile and previous.name.startswith("account-"):
                    try:
                        self._run("stop", home=previous, allow_empty=True)
                    except NeteaseError:
                        pass
                    shutil.rmtree(previous, ignore_errors=True)
                    shutil.rmtree(self._runtime_path(previous), ignore_errors=True)
            except OSError:
                raise NeteaseError("无法保存网易云音乐配置。") from None
            finally:
                link.unlink(missing_ok=True)
                if not committed:
                    shutil.rmtree(stage, ignore_errors=True)
                    shutil.rmtree(self._runtime_path(stage), ignore_errors=True)

    def login(self):
        with self._lock:
            pending = self._pending_login
            self._pending_login = None
            if pending and time.monotonic() - pending[0] < 120:
                return pending[1]
            return self._qr(self._run("login", "--background"))

    def account_status(self):
        try:
            profile = self._data("user", "info")
        except NeteaseError:
            return self.login_status()
        return {"success": True, "user": profile}

    def login_status(self):
        with self._lock:
            result = self._run("login", "--check", allow_failure=True)
            logged_in = isinstance(result, dict) and result.get("success") is True
            if logged_in:
                self._pending_login = None
                status = {"success": True, "loggedIn": True}
                for user in (result.get("user"), result.get("userInfo"),
                             result.get("profile"), result.get("data"), result):
                    if isinstance(user, dict):
                        nickname = user.get("nickname") or user.get("name")
                        if isinstance(nickname, str) and nickname:
                            status["user"] = {"nickname": nickname}
                            break
                return status
            message = result.get("message", "") if isinstance(result, dict) else ""
            error = self._error({"message": message})
            if not error.reauth:
                raise error
            return {"success": False, "loggedIn": False}

    def logout(self):
        with self._lock:
            self._run("logout", allow_empty=True)
            self._pending_login = None
            with self._media_lock:
                self._media_urls.clear()

    @staticmethod
    def _record(record):
        item = dict(record)
        for field in ("id", "originalId"):
            if item.get(field) is not None:
                item[field] = str(item[field])
        return item

    def _dynamic(self, *args):
        try:
            return self._run(*args)
        except NeteaseError as error:
            if error.check_session:
                try:
                    logged_in = self.login_status()["loggedIn"]
                except NeteaseError:
                    raise error from None
                if not logged_in:
                    raise NeteaseError("请先扫码登录网易云音乐。", reauth=True) from None
            raise

    def _data(self, *args):
        payload = self._dynamic(*args)
        if isinstance(payload, dict) and payload.get("code", 200) == 200 and "data" in payload:
            return payload["data"]
        raise NeteaseError("网易云音乐返回了无法识别的数据。")

    def _records(self, *args):
        data = self._data(*args)
        if isinstance(data, dict):
            data = data.get("records")
        if not isinstance(data, list):
            raise NeteaseError("网易云音乐返回了无法识别的列表。")
        return [self._record(item) for item in data if isinstance(item, dict)]

    def search(self, query):
        query = query.strip()
        if not query:
            return []
        return self._records("search", "song", f"--keyword={query}")

    def library(self, kind):
        if kind == "daily":
            return self._records("recommend", "daily")
        if kind == "favorite":
            data = self._data("user", "favorite")
            return [self._record(data)] if isinstance(data, dict) and data.get("id") else []
        if kind in ("created", "collected"):
            return self._records("playlist", kind)
        raise NeteaseError("未知的音乐库类型。")

    def tracks(self, playlist_id, offset=0):
        return self._records(
            "playlist", "tracks", f"--playlistId={playlist_id}",
            "--limit", str(PLAYLIST_PAGE_SIZE), "--offset", str(offset),
        )

    def lyrics(self, song_id):
        data = self._data("song", "lyric", f"--songId={song_id}")
        if isinstance(data, dict):
            return str(data.get("lyric") or data.get("txtLyric") or "")
        raise NeteaseError("网易云音乐返回了无法识别的歌词。")

    def like(self, song_id, liked):
        self._dynamic("song", "like" if liked else "dislike", f"--songId={song_id}")

    def _track_location(self, track):
        if track.get("visible") is False:
            raise NeteaseError("当前歌曲暂不可播放。")
        if not track.get("id") or not track.get("originalId"):
            raise NeteaseError("这首歌缺少官方播放器需要的歌曲 ID。")
        return self._current.resolve(strict=True), (str(track["id"]), str(track["originalId"]))

    def _cached_media(self, key):
        with self._media_lock:
            cached = self._media_urls.get(key)
            if cached is None:
                return None
            if cached[1] <= time.time() * 1000 + 5000:
                self._media_urls.pop(key)
                return None
            self._media_urls.move_to_end(key)
            return cached[0]

    def is_preloaded(self, track):
        key = str(track["id"]), str(track["originalId"])
        return self._cached_media(key) is not None

    def preload(self, track):
        home, key = self._track_location(track)
        if self._cached_media(key) is None:
            self._fetch_media(home, key)

    def _try_cached_play(self, home, key, volume):
        url = self._cached_media(key)
        if url is None:
            return False
        try:
            self._play_url(home, url, volume)
        except MpvError:
            with self._media_lock:
                self._media_urls.pop(key, None)
            return False
        return True

    def play_cached(self, track, *, volume=75):
        home, key = self._track_location(track)
        return self._try_cached_play(home, key, volume)

    def play(self, track, *, volume=75):
        home, key = self._track_location(track)
        if self._try_cached_play(home, key, volume):
            return
        url = self._fetch_media(home, key)
        try:
            self._play_url(home, url, volume)
        except MpvError as error:
            with self._media_lock:
                self._media_urls.pop(key, None)
            raise NeteaseError(str(error)) from None

    def _fetch_media(self, home, key):
        with tempfile.TemporaryDirectory(prefix="media-", dir=self._runtime(home)) as staging:
            media_file = Path(staging) / "response.json"
            self._run("play", "--song", "--encrypted-id", key[0], "--original-id", key[1],
                      allow_empty=True, media_file=media_file)
            payload = self._wait_for_media(media_file)
        url = payload.get("url")
        try:
            location = urlsplit(url) if isinstance(url, str) else None
            if (location is None or location.scheme not in ("http", "https") or
                    not location.hostname or location.username is not None):
                raise ValueError
        except ValueError:
            raise NeteaseError("网易云音乐没有返回可用播放地址。") from None
        expiry = payload.get("expiresAt")
        if isinstance(expiry, (int, float)) and expiry > time.time() * 1000 + 5000:
            with self._media_lock:
                self._media_urls[key] = (url, expiry)
                if len(self._media_urls) > 6:
                    self._media_urls.popitem(last=False)
        return url

    def _play_url(self, home, url, volume):
        if self._player is None:
            self._player = Mpv(self._runtime(home) / "zeromusic-audio.sock", lambda: self._closing)
        self._player.play(url, volume)

    def _wait_for_media(self, media_file):
        end = time.monotonic() + 24
        while time.monotonic() < end and not self._closing:
            try:
                payload = json.loads(media_file.read_text())
            except (FileNotFoundError, ValueError):
                time.sleep(0.15)
                continue
            if isinstance(payload, dict):
                if payload.get("network"):
                    raise NeteaseError("连接网易云音乐失败，请重试播放。")
                return payload
        raise NeteaseError("网易云音乐播放请求超时，请重试。")

    def state(self):
        player = self._player
        return player.state() if player else {"status": "stopped"}

    def control(self, command, *args):
        player = self._player
        if command == "stop":
            if player:
                player.close()
                self._player = None
            return
        if player is None:
            raise NeteaseError("当前没有正在播放的歌曲。")
        try:
            if command in ("pause", "resume"):
                player.command("set_property", "pause", command == "pause")
            elif command == "volume":
                player.command("set_property", "volume", int(args[0]))
            else:
                raise NeteaseError("未知的播放操作。")
        except MpvError as error:
            raise NeteaseError(str(error)) from None
