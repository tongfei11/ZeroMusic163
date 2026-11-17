"""Keyboard interaction and non-blocking CLI orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from random import sample
from time import monotonic

from artwork import download_thumbnail

from netease import NeteaseError, PLAYLIST_PAGE_SIZE, memory_available
from playback_resume import load_resume, save_resume
from player import MODES, Player, PlaylistTail, artists, lyric_lines, number, playable, track_rows
from setup_web import SetupWeb


TABS = ("搜索", "歌单", "播放", "队列")
LIBRARY = [("喜欢的音乐", "favorite"), ("我的歌单", "created"), ("每日推荐", "daily"), ("设置", "settings")]
SETTINGS = [("隐藏不可播放曲目", "hide-unplayable"), ("后台播放", "background"),
            ("初始化", "setup"), ("切换账号", "login")]
MOVES = {"f": -1, "up": -1, "x": 1, "down": 1}
VOLUME = {"s": -5, "volume-down": -5, "d": 5, "volume-up": 5}
PRELOAD_TRACKS = 3
PLAYBACK_HELP = [
    ("4 模式 · 5 上一首 · 6 播放 / 暂停", ""),
    ("7 下一首 · 8 收藏 / 取消收藏", ""),
    ("S / D 调音量 · Tab 换页", ""),
    ("H / Esc 关闭帮助 · 长按 Esc 退出", ""),
]


@dataclass
class Page:
    title: str
    kind: str
    items: list
    selected: int = 0
    playlist_id: str = ""
    has_more: bool = False
    liked: bool = False


@dataclass
class Search:
    query: str = ""
    editing: bool = True
    generation: int = 0
    tracks: list = field(default_factory=list)
    selected: int = 0


@dataclass
class PlaybackRetry:
    track: dict
    remaining: list
    position: float = 0
    previous: bool = False
    automatic: bool = False
    attempt: int = 0
    at: float | None = None


class App:
    def __init__(self, client, web=SetupWeb, clock=monotonic, settings_path=None, *, background=False):
        self.client, self.web_factory, self.clock = client, web, clock
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ncm")
        self.images = ThreadPoolExecutor(max_workers=1, thread_name_prefix="images")
        self.audio = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio")
        self.downloads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prefetch")
        self.settings_path = Path(settings_path) if settings_path else None
        self.resume_path = self.settings_path.with_name("playback.json") if self.settings_path else None
        settings = load_resume(self.settings_path) if self.settings_path else {}
        self.hide_unplayable = settings.get("hide_unplayable") is True
        self.background_playback = settings.get("background_playback") is True
        self.background = background
        self.resume_data = load_resume(self.resume_path) if self.resume_path else {}
        self.next_save = self.clock() + 15
        self.jobs = {}
        self.library_loading = None
        self.library_request = None
        self.running = True
        self.screen = "checking"
        self.return_screen = "ready"
        self.tab, self.esc_started = 0, None
        self.search = Search()
        self.library = [Page("歌单", "menu", LIBRARY)]
        self.help_visible = False
        self.confirmation = None
        self.web = None
        self.qr, self.qr_deadline = "", 0
        self.login_completed = False
        self.account_generation = 0
        self.login_attempt = 0
        self.account = ""
        self.avatar_url, self.avatar = "", None
        self.profile_at = None
        self.artwork_url, self.artwork = "", None
        self.pending_track = None
        self.pending_queue = None
        self.play_retry = None
        self.buffering_since = None
        self.position_unknown_since = None
        self.row_scroll_key = None
        self.row_scroll_started = self.clock()
        self.player = Player()
        self.track_started_at = 0
        self.title_started_at = self.clock()
        self.queue_selection = 0
        self.queue_flip = None
        self.queue_advance = None
        self.lyric_cache = {}
        self.like_pending = None
        self.requested_volume = None
        self.preload_attempt = set()
        self.audio_prefetch_attempt = None
        self.shuffle_next = []
        self.last_key_at = self.clock()
        self.next_poll = 0
        self.notice_text, self.notice_until = "", 0
        self.start()

    def submit(self, key, action, success, failure=None, *, image=False, cached=False, download=False):
        if key not in self.jobs:
            if image:
                executor = self.images
            elif download:
                executor = self.downloads
            elif cached or key in ("position", "control", "volume"):
                executor = self.audio
            else:
                executor = self.pool
            self.jobs[key] = (executor.submit(action), success, failure)

    def tick(self):
        now = self.clock()
        if self.esc_started is not None and now - self.esc_started >= 3:
            self.running = False
        if not self.running:
            return
        completed = [(key, job) for key, job in self.jobs.items() if job[0].done()]
        for key, (future, success, failure) in completed:
            del self.jobs[key]
            try:
                result = future.result()
            except NeteaseError as error:
                if failure is not None:
                    failure(error)
                else:
                    self.error(error)
            else:
                success(result)
        self.run_library_request()
        if self.screen == "ready":
            self.maintain_queue()
            if self.play_retry and self.play_retry.at is not None and now >= self.play_retry.at:
                self.retry_playback()

        if self.screen == "setup" and self.web and self.web.saved.is_set():
            self.close_web()
            self.account_generation += 1
            self.player = Player()
            self.play_retry = None
            self.library = [Page("歌单", "menu", LIBRARY)]
            self.search = Search()
            self.forget_session()
            self.login_completed = False
            self.clear_account()
            self.request_login()
        if self.screen == "login" and self.qr and now >= self.qr_deadline:
            self.qr = ""
            self.notice("二维码已过期，按 Enter 刷新", seconds=300)
        if (self.profile_at is not None and now >= self.profile_at
                and self.screen == "ready" and self.tab == 1 and not self.jobs):
            self.profile_at = None
            self.submit("profile", self.client.account_status, self.apply_account, lambda _error: None)
        if self.screen == "ready" and now - self.last_key_at >= 2:
            self.load_playback_extras()
        if self.screen == "ready" and self.player.current and now >= self.next_save:
            self.save_session(live=self.should_keep_playing or self.player.status == "paused")
        if now < self.next_poll:
            return
        if self.screen == "login" and self.qr:
            self.next_poll = now + 2.5
            attempt = self.login_attempt
            self.submit("login-status", self.client.login_status,
                        lambda data: self.finish_login(data) if attempt == self.login_attempt else None,
                        lambda _error: None)
        if self.screen == "ready" and self.player.current and self.player.status in ("starting", "playing", "paused", "buffering"):
            if not {"play", "control", "volume"} & self.jobs.keys():
                self.next_poll = now + 0.9
                self.submit("position", self.client.state, self.position_changed, self.position_error)

    def notice(self, message, seconds=4):
        self.notice_text, self.notice_until = message, self.clock() + seconds

    def error(self, error):
        if getattr(error, "reauth", False) and self.screen == "ready":
            self.play_retry = None
            self.login_completed = False
            if self.player.current and self.player.status in ("playing", "paused", "buffering"):
                self.notice("登录已失效，请在设置中切换账号", seconds=15)
                return
            self.screen, self.qr = "signed-out", ""
        self.notice(str(error), seconds=7)

    def start(self):
        self.screen = "checking"
        if self.client.configured():
            if self.resume_data.get("live") is True:
                self.restore_session()
                if self.should_keep_playing or self.player.status == "paused":
                    self.screen = "ready"
            if self.background:
                self.screen = "ready"
                return
            self.submit("login-status", self.client.account_status, self.initial_login, self.login_error)
        else:
            self.open_setup()

    def initial_login(self, data):
        if not self.finish_login(data):
            self.request_login()

    def restore_session(self):
        saved, self.resume_data = self.resume_data, {}
        track = saved.get("track")
        if not isinstance(track, dict) or not playable(track):
            return
        player = self.player
        player.current = track
        player.upcoming = restore_queue(saved.get("upcoming"))
        player.history = [entry for entry in restore_queue(saved.get("history")) if isinstance(entry, dict)]
        player.mode = saved.get("mode") if saved.get("mode") in (0, 1, 2) else 0
        player.volume = int(min(100, number(saved.get("volume", 75))))
        player.position, player.duration = number(saved.get("position")), number(saved.get("duration"))
        player.status = "resume"
        self.tab = 2
        self.title_started_at = self.clock()
        if not self.background:
            self.show_artwork(track)
        if saved.get("live") is True:
            state = self.client.adopt_playback()
            if state and state["status"] in ("playing", "paused", "buffering"):
                player.apply_status(state)
            elif state and not saved.get("retry") and (saved.get("was_playing") is True or saved.get("continue_queue") is True):
                player.status = "ended"
                player.position = player.duration
                if player.mode == 2:
                    self.start_track(track, player.upcoming[:])
                else:
                    self.advance(automatic=True)
        retry = saved.get("retry")
        if isinstance(retry, dict) and isinstance(retry.get("track"), dict) and playable(retry["track"]):
            self.play_retry = PlaybackRetry(
                retry["track"], restore_queue(retry.get("remaining")), number(retry.get("position")),
                retry.get("previous") is True, retry.get("automatic") is True,
                min(6, int(number(retry.get("attempt")))),
            )
            if saved.get("live") is True and isinstance(retry.get("delay"), (int, float)):
                self.play_retry.at = self.clock() + min(30, number(retry["delay"]))
            self.notice("等待网络恢复 · 6 重试 · Esc 取消", seconds=300)
        if player.status == "resume":
            self.notice("按 6 继续播放")

    def save_session(self, *, live):
        self.next_save = self.clock() + 15
        player = self.player
        if self.resume_path is None or not player.current:
            return
        recovery = self.play_retry
        retry = None
        if recovery:
            delay = max(0, recovery.at - self.clock()) if recovery.at is not None else (
                3 if self.pending_track else None)
            retry = {"track": recovery.track, "remaining": [save_queue_entry(entry) for entry in recovery.remaining],
                     "position": recovery.position, "previous": recovery.previous, "automatic": recovery.automatic,
                     "attempt": recovery.attempt, "delay": delay}
        try:
            save_resume(self.resume_path, {
                "track": player.current, "upcoming": [save_queue_entry(entry) for entry in player.upcoming],
                "history": player.history[-30:], "position": player.position if player.status != "ended" else 0,
                "duration": player.duration, "mode": player.mode, "volume": player.volume,
                "live": live, "was_playing": player.status in ("starting", "playing", "buffering"),
                "continue_queue": self.queue_advance is not None,
                "retry": retry,
            })
        except OSError:
            self.notice("无法保存播放进度")

    def forget_session(self):
        self.resume_data = {}
        self.queue_advance = self.queue_flip = None
        if self.resume_path:
            try:
                self.resume_path.unlink(missing_ok=True)
            except OSError:
                self.notice("无法清除上次播放进度")

    @property
    def should_keep_playing(self):
        return (self.player.status in ("playing", "starting", "buffering") or self.pending_track is not None
                or self.queue_advance is not None
                or bool(self.play_retry and self.play_retry.at is not None))

    @property
    def background_finished(self):
        return not (self.should_keep_playing or self.pending_track or self.player.status == "paused")

    def finish_login(self, data):
        if not data.get("success"):
            return False
        self.apply_account(data)
        self.profile_at = None if data.get("user") else self.clock() + 2
        self.login_completed = True
        self.screen, self.qr = "ready", ""
        self.notice("")
        if self.resume_data:
            self.restore_session()
        elif self.player.status == "resume":
            self.notice("按 6 继续播放")
        return True

    def apply_account(self, data):
        user = data.get("user")
        if not isinstance(user, dict):
            self.account = "已登录"
            return
        self.account = user.get("nickname") or "已登录"
        url = user.get("avatarUrl")
        if not isinstance(url, str) or not url or url == self.avatar_url:
            return
        self.avatar_url, self.avatar = url, None

        def done(image):
            if self.avatar_url == url:
                self.avatar = image

        self.submit(f"avatar-{url}", partial(download_thumbnail, url, 48), done, image=True)

    def clear_account(self):
        self.account = ""
        self.avatar_url, self.avatar = "", None
        self.profile_at = None
        self.artwork_url, self.artwork = "", None
        self.lyric_cache.clear()
        self.preload_attempt.clear()
        self.audio_prefetch_attempt = None
        self.shuffle_next.clear()

    def show_artwork(self, track):
        album = track.get("album")
        cover = track.get("coverImgUrl") or (album.get("coverImgUrl") if isinstance(album, dict) else None)
        url = cover if isinstance(cover, str) else ""
        if url == self.artwork_url:
            return
        self.artwork_url, self.artwork = url, None
        if not url:
            return

        def done(image):
            if self.artwork_url == url:
                self.artwork = image

        self.submit(f"cover-{url}", partial(download_thumbnail, url, 80), done, image=True)

    def visible_tracks(self, tracks):
        return [track for track in tracks if playable(track)] if self.hide_unplayable else tracks

    def toggle_setting(self, name):
        settings = {key: getattr(self, key) for key in ("hide_unplayable", "background_playback")}
        settings[name] = not settings[name]
        if self.settings_path:
            try:
                save_resume(self.settings_path, settings)
            except OSError:
                self.notice("无法保存设置")
                return
        setattr(self, name, settings[name])
        if name == "hide_unplayable":
            self.search.selected = 0

    def login_error(self, error):
        if self.player.current and self.player.status in ("playing", "paused", "buffering", "resume"):
            self.screen = "ready"
        else:
            self.screen = "signed-out" if error.reauth else "offline"
        self.qr = ""
        self.error(error)

    def open_setup(self):
        self.return_screen = self.screen if self.screen in ("ready", "signed-out") else "need-setup"
        self.screen = "setup"
        try:
            self.web = self.web_factory(self.client.configure, configured=self.client.configured())
        except OSError:
            self.screen = self.return_screen
            self.notice("无法开启配置网页，按 Enter 重试")

    def close_web(self):
        if self.web:
            self.web.close()
            self.web = None

    def request_login(self):
        if "login" in self.jobs:
            return
        self.screen = "login"
        self.library_request = self.library_loading = None
        self.login_attempt += 1
        self.qr = ""
        self.notice("")
        self.submit("login", self.client.login, self.show_qr, self.login_error)

    def show_qr(self, url):
        if self.screen == "login":
            self.qr, self.qr_deadline = url, self.clock() + 300
            self.next_poll = 0

    def key(self, key, value):
        if value == 1:
            self.last_key_at = self.clock()
        if key == "esc":
            if value == 1:
                self.esc_started = self.clock()
            elif value == 0 and self.esc_started is not None:
                elapsed, self.esc_started = self.clock() - self.esc_started, None
                if elapsed >= 3:
                    self.running = False
                else:
                    self.back()
            return
        repeatable = key in (*MOVES, "backspace") or (self.tab == 2 and key in VOLUME)
        if not value or (value == 2 and not repeatable):
            return
        if self.confirmation:
            if key in MOVES:
                self.confirmation[2] = 1 - self.confirmation[2]
            elif key == "enter":
                _, action, selected = self.confirmation
                self.confirmation = None
                if selected == 1:
                    action()
            return
        if self.screen != "ready":
            if key == "enter" and self.screen == "offline":
                self.start()
            elif key == "enter" and self.screen == "need-setup":
                self.open_setup()
            elif key == "enter" and self.screen in ("login", "signed-out"):
                self.request_login()
            elif key == "a" and self.screen in ("login", "signed-out", "offline"):
                self.confirmation = ["清除配置、登录和播放记录？", self.initialize, 0]
            return
        if key == "tab":
            self.help_visible = False
            self.tab = (self.tab + 1) % len(TABS)
            return
        if self.tab == 0:
            self.search_key(key)
        elif self.tab == 1:
            self.library_key(key, self.library[-1])
        elif self.tab == 2:
            self.play_key(key)
        else:
            self.queue_key(key)

    def back(self):
        if self.confirmation:
            self.confirmation = None
        elif self.screen == "setup":
            self.close_web()
            self.screen = self.return_screen
        elif self.screen == "login":
            self.login_attempt += 1
            self.qr = ""
            self.screen = "ready" if self.login_completed else "signed-out"
        elif self.screen == "ready":
            if self.confirmation:
                self.confirmation = None
            elif self.tab == 0 and (not self.search.editing or self.has_search_job()):
                self.search.generation += 1
                self.search.editing = True
                self.notice("")
            elif self.tab == 1:
                self.library_request = self.library_loading = None
                if len(self.library) > 1:
                    self.library.pop()
            elif self.tab == 2:
                self.help_visible = False
                if self.play_retry:
                    self.play_retry = None
                    self.notice("已取消重连")

    def has_search_job(self):
        return f"search-{self.search.generation}" in self.jobs

    def search_key(self, key):
        search = self.search
        if search.editing:
            if key == "enter":
                self.request_search()
            elif key == "backspace":
                search.query = search.query[:-1]
                self.search_edited()
            elif key == "space" or (len(key) == 1 and key.isascii() and key.isalnum()):
                if len(search.query) < 80:
                    search.query += " " if key == "space" else key
                    self.search_edited()
        else:
            tracks = self.visible_tracks(search.tracks)
            if tracks:
                search.selected = moved(search.selected, key, len(tracks))
                self.track_key(key, tracks[search.selected])

    def search_edited(self):
        self.search.generation += 1
        self.notice("")

    def request_search(self):
        search = self.search
        keyword, generation = search.query.strip(), search.generation
        if not keyword:
            self.notice("输入歌名或歌手全拼")
            return

        def done(tracks):
            if generation == search.generation:
                search.tracks, search.selected, search.editing = tracks, 0, not tracks
                self.notice("" if tracks else "没找到歌曲，试试完整拼音", seconds=6)

        def failed(error):
            if generation == search.generation:
                self.error(error)

        self.submit(f"search-{generation}", partial(self.client.search, keyword), done, failed)

    def library_key(self, key, page):
        if page.kind == "tracks":
            tracks = self.visible_tracks(page.items)
            rows = len(tracks) + bool(tracks)
            page.selected = moved(page.selected, key, rows + page.has_more)
            if page.has_more and page.selected == rows and key == "enter":
                self.load_more_tracks(page)
            elif tracks:
                if page.selected == 0 and key in ("enter", "a"):
                    self.use_collection(page, play=key == "enter")
                elif 0 < page.selected < rows:
                    self.track_key(key, tracks[page.selected - 1])
            return
        if not page.items:
            return
        page.selected = moved(page.selected, key, len(page.items))
        if key == "space":
            self.toggle_playback()
        if key not in ("enter", "a"):
            return
        item = page.items[page.selected]
        if page.kind == "playlists":
            fetch = partial(self.playlist_page, item.get("name") or "歌单", item["id"])
            self.open_list(fetch, page, enqueue=key == "a")
        else:
            label, action = item
            if key == "a":
                if action in ("favorite", "daily"):
                    self.open_list(partial(self.collection_page, label, action), page, enqueue=True)
                return
            if action == "settings":
                self.library_request = self.library_loading = None
                self.library.append(Page("设置", "settings", SETTINGS))
            elif action == "hide-unplayable":
                self.toggle_setting("hide_unplayable")
            elif action == "background":
                self.toggle_setting("background_playback")
            elif action == "setup":
                self.confirmation = ["清除配置、登录和播放记录？", self.initialize, 0]
            elif action == "login":
                self.confirmation = ["停止播放并重新扫码授权？", self.change_account, 0]
            else:
                self.open_list(partial(self.collection_page, label, action), page)

    def collection_page(self, title, kind):
        items = self.client.library(kind)
        if kind == "created":
            combined = items + self.client.library("collected")
            return Page(title, "playlists",
                        list({item["id"]: item for item in combined if item.get("id")}.values()))
        if kind == "favorite" and items:
            return self.playlist_page(title, items[0]["id"], liked=True)
        return Page(title, "tracks", items)

    def playlist_page(self, title, playlist_id, liked=False):
        items = self.client.tracks(playlist_id)
        if liked:
            items = [dict(track, liked=True) for track in items]
        return Page(title, "tracks", items, playlist_id=playlist_id,
                    has_more=len(items) == PLAYLIST_PAGE_SIZE, liked=liked)

    def submit_library(self, page, fetch, done, failed, *, intent="enter"):
        if self.library_request and self.library_request[:3] == (id(page), page.selected, intent):
            return
        self.library_request = (id(page), page.selected, intent, fetch, done, failed)
        self.library_loading = (page, page.selected, self.clock())
        self.run_library_request()

    def run_library_request(self):
        request = self.library_request
        if request is None or "library" in self.jobs:
            return
        _, _, _, fetch, done, failed = request

        def finish(callback, result):
            if self.library_request is request:
                self.library_request = self.library_loading = None
                if self.screen == "ready":
                    callback(result)

        self.submit("library", fetch, partial(finish, done), partial(finish, failed))

    def open_list(self, fetch, parent, *, enqueue=False):
        def still_selected():
            return self.library[-1] is parent

        def done(page):
            if still_selected():
                if enqueue:
                    self.use_collection(page)
                else:
                    self.library.append(page)
                    self.notice("")

        def failed(error):
            if still_selected():
                self.error(error)

        self.submit_library(parent, fetch, done, failed, intent="a" if enqueue else "enter")

    def load_more_tracks(self, page):
        offset = len(page.items)

        def done(items):
            if self.library[-1] is not page:
                return
            if items and [track.get("id") for track in items] == [
                track.get("id") for track in page.items[-len(items):]
            ]:
                page.has_more = False
                self.notice("已到歌单末尾")
            else:
                page.items.extend(dict(track, liked=True) if page.liked else track for track in items)
                page.has_more = len(items) == PLAYLIST_PAGE_SIZE
                self.notice("" if items else "已到歌单末尾")
            shown = len(self.visible_tracks(page.items))
            page.selected = min(page.selected, max(0, shown + page.has_more))

        def failed(error):
            if self.library[-1] is page:
                self.error(error)

        self.submit_library(page, partial(self.client.tracks, page.playlist_id, offset), done, failed)

    def change_account(self):
        self.screen = "checking"
        self.play_retry = None
        self.library_request = self.library_loading = None
        had_track = self.player.current is not None
        self.account_generation += 1
        pending = self.jobs.get("play")

        def logout():
            if pending:
                try:
                    pending[0].result(timeout=35)
                except (NeteaseError, TimeoutError):
                    pass
            if had_track or pending:
                self.client.control("stop")
            self.client.logout()

        def done(_result):
            self.player = Player()
            self.pending_track = self.pending_queue = None
            self.forget_session()
            self.library = [Page("歌单", "menu", LIBRARY)]
            self.search = Search()
            self.tab, self.login_completed = 0, False
            self.clear_account()
            self.request_login()

        def failed(error):
            self.screen = "ready"
            if had_track:
                self.player.status = "resume"
            self.error(error)

        self.submit("logout", logout, done, failed)

    def initialize(self):
        self.screen = "checking"
        self.account_generation += 1
        self.play_retry = self.library_request = self.library_loading = None
        self.close_web()
        pending = self.jobs.get("play")

        def reset():
            if pending:
                try:
                    pending[0].result(timeout=35)
                except (NeteaseError, TimeoutError):
                    pass
            self.client.reset()

        def done(_result):
            self.player = Player()
            self.pending_track = self.pending_queue = None
            self.requested_volume = None
            self.search = Search()
            self.library = [Page("歌单", "menu", LIBRARY)]
            self.login_completed = False
            self.hide_unplayable = self.background_playback = False
            self.resume_data = {}
            self.clear_account()
            self.tab, self.screen = 0, "need-setup"
            self.open_setup()

        def failed(error):
            self.screen = "need-setup" if not self.client.configured() else "ready"
            self.error(error)

        self.submit("initialize", reset, done, failed)

    def append_to_queue(self, entries):
        self.player.upcoming.extend(entries)
        if self.pending_queue is not None and self.pending_queue is not self.player.upcoming:
            self.pending_queue.extend(entries)
        if (self.play_retry and self.play_retry.remaining is not self.player.upcoming
                and self.play_retry.remaining is not self.pending_queue):
            self.play_retry.remaining.extend(entries)

    def track_key(self, key, track):
        if key == "enter":
            self.start_track(track, self.player.upcoming[:], switch=True)
        elif key == "space":
            self.toggle_playback()
        elif key == "a" and "play" not in self.jobs:
            if not playable(track):
                self.notice("这首歌暂不可播放")
                return
            self.append_to_queue([track])
            self.notice("已加入队列")

    def use_collection(self, page, *, play=False):
        entries = [track for track in page.items if playable(track)]
        if page.has_more:
            ids = tuple(track.get("id") for track in page.items[-PLAYLIST_PAGE_SIZE:])
            entries.append(PlaylistTail(page.title, page.playlist_id, len(page.items), ids, page.liked))
        if not entries:
            self.notice("没有可播放的歌曲")
            return
        if not play:
            self.append_to_queue(entries)
            self.notice("已整单加入队列")
            return

        def action():
            if "play" in self.jobs:
                return
            first, *remaining = entries
            if isinstance(first, PlaylistTail):
                self.player.upcoming = entries
                self.advance(switch=True)
            else:
                self.start_track(first, remaining, switch=True)

        if self.player.upcoming:
            self.confirmation = ["替换当前队列？", action, 0]
        else:
            action()

    def start_track(self, track, remaining, *, switch=False, previous=False, automatic=False, position=0, retry=False):
        if not playable(track):
            self.notice("这首歌暂不可播放")
            return
        if "logout" in self.jobs or "play" in self.jobs:
            return
        if not retry:
            self.play_retry = None
        self.queue_advance = self.queue_flip = None
        volume = self.player.volume
        generation = self.account_generation
        self.pending_track = track
        self.pending_queue = remaining
        if track is not self.player.current:
            self.title_started_at = self.clock()
        self.help_visible = False
        if not self.background:
            self.show_artwork(track)
        if switch:
            self.tab = 2

        def done(_result):
            if generation != self.account_generation:
                return
            self.play_retry = None
            self.buffering_since = None
            self.pending_track = None
            self.pending_queue = None
            if previous:
                if self.player.history:
                    self.player.history.pop()
            self.player.jump(track, remaining, remember=not previous)
            self.player.status = "playing"
            self.player.position = position
            self.track_started_at, self.next_poll = self.clock(), 0
            self.queue_selection = 0
            self.preload_attempt.clear()
            self.notice("")
            self.save_session(live=True)
            if self.player.volume != volume:
                self.submit("control", partial(self.client.control, "volume", str(self.player.volume)), lambda _: None)

        def failed(error):
            if generation != self.account_generation:
                return
            self.pending_track = None
            self.pending_queue = None
            if self.player.current and not self.background:
                self.show_artwork(self.player.current)
            if not self.player.current:
                self.player.current = track
                self.player.status = "failed"
                self.player.upcoming = remaining
            if error.retryable:
                self.schedule_playback_retry(track, remaining, position=position,
                                             previous=previous, automatic=automatic)
                return
            self.error(error)
            if automatic and error.unavailable:
                self.play_retry = None
                self.player.upcoming = remaining
                self.advance(automatic=True)

        def resolve_and_play():
            if generation == self.account_generation:
                self.submit("play", partial(self.client.play, track, **options), done, failed)

        def cached_result(played):
            if played:
                done(None)
            else:
                resolve_and_play()

        self.notice("")
        options = {"volume": volume}
        if position:
            options["position"] = position
        if self.client.is_preloaded(track):
            self.submit("play", partial(self.client.play_cached, track, **options),
                        cached_result, failed, cached=True)
        else:
            resolve_and_play()

    def schedule_playback_retry(self, track, remaining, *, position=0, previous=False, automatic=False):
        recovery = self.play_retry
        if recovery is None or recovery.track is not track:
            recovery = PlaybackRetry(track, remaining, position, previous, automatic)
            self.play_retry = recovery
        recovery.attempt += 1
        if recovery.attempt > 5:
            recovery.at = None
            self.notice("重连失败 · 6 重试 · 7 下一首", seconds=300)
        else:
            delay = min(30, 3 * 2 ** (recovery.attempt - 1))
            recovery.at = self.clock() + delay
            self.notice(f"连接中断 · {delay}秒后重试 · Esc 取消", seconds=delay + 30)

    def retry_playback(self):
        recovery = self.play_retry
        if recovery is None or "play" in self.jobs or "logout" in self.jobs:
            return
        recovery.at = None
        self.start_track(recovery.track, recovery.remaining, position=recovery.position,
                         previous=recovery.previous, automatic=recovery.automatic, retry=True)

    def position_changed(self, state):
        if {"play", "control", "volume"} & self.jobs.keys():
            return
        if state.get("status") == "unknown":
            if self.position_unknown_since is None:
                self.position_unknown_since = self.clock()
            elif self.clock() - self.position_unknown_since >= 6 and not self.play_retry:
                self.player.status = "failed"
                self.schedule_playback_retry(self.player.current, self.player.upcoming[:], position=self.player.position)
            return
        self.position_unknown_since = None
        if self.player.apply_status(state):
            if self.player.mode == 2:
                self.start_track(self.player.current, self.player.upcoming[:])
            else:
                self.advance(automatic=True)
        elif self.player.status == "buffering":
            if self.buffering_since is None:
                self.buffering_since = self.clock()
                self.notice("网络缓冲中 · 6 暂停 · 7 下一首", seconds=300)
            elif self.clock() - self.buffering_since > 45 and not self.play_retry:
                self.notice("缓冲较久 · Enter 重连 · 7 下一首", seconds=300)
        elif self.player.status == "playing" and self.buffering_since is not None:
            self.buffering_since = None
            self.notice("播放已恢复", seconds=2)
        elif self.player.status == "starting" and self.clock() - self.track_started_at > 10:
            self.player.status = "failed"
            if not self.play_retry:
                self.schedule_playback_retry(self.player.current, self.player.upcoming[:], position=self.player.position)
        elif self.player.status == "failed" and not self.play_retry:
            self.schedule_playback_retry(self.player.current, self.player.upcoming[:], position=self.player.position)

    def position_error(self, error):
        if getattr(error, "reauth", False):
            self.error(error)
        elif self.player.status == "starting" and self.clock() - self.track_started_at > 10:
            self.player.status = "failed"
            self.error(error)

    def queue_boundary(self):
        upcoming = self.player.upcoming
        for index, entry in enumerate(upcoming):
            if isinstance(entry, PlaylistTail):
                return index, entry
        return len(upcoming), None

    def next_indices(self):
        count, _ = self.queue_boundary()
        if self.player.mode != 1:
            return range(min(count, PRELOAD_TRACKS))
        upcoming = self.player.upcoming
        indices = {id(upcoming[index]): index for index in range(count)}
        self.shuffle_next = [key for key in self.shuffle_next if key in indices]
        candidates = [key for key in indices if key not in self.shuffle_next]
        self.shuffle_next.extend(sample(candidates, min(PRELOAD_TRACKS - len(self.shuffle_next), len(candidates))))
        return [indices[key] for key in self.shuffle_next]

    def maintain_queue(self):
        if "play" in self.jobs:
            return
        upcoming = self.player.upcoming
        index, source = self.queue_boundary()
        if source is not None and source.fetched is not None:
            items, source.fetched = source.fetched, None
            ids = tuple(track.get("id") for track in items)
            tracks = []
            if ids != source.previous_ids:
                tracks = [dict(track, liked=True) if source.liked else track for track in items if playable(track)]
            has_more = len(items) == PLAYLIST_PAGE_SIZE and ids != source.previous_ids
            source.offset += len(items)
            source.previous_ids = ids
            replacement = tracks + ([source] if has_more else [])
            upcoming[index:index + 1] = replacement
            if self.play_retry:
                remaining = self.play_retry.remaining
                for source_index, entry in enumerate(remaining):
                    if entry is source:
                        remaining[source_index:source_index + 1] = replacement
                        break
            index, source = self.queue_boundary()
        if self.queue_flip is not None:
            target = self.queue_flip
            size = index + int(self.player.current is not None)
            if target < size or source is None:
                self.queue_selection = max(0, min(target, size - 1))
                self.queue_flip = None
                self.notice("")
        if self.queue_advance is not None and (index or source is None):
            automatic, switch = self.queue_advance
            self.queue_advance = None
            self.advance(automatic=automatic, switch=switch)
            return
        if source is None or self.clock() < source.retry_at:
            return
        if index <= PRELOAD_TRACKS or self.queue_flip is not None:
            if any(key not in ("position", "control", "volume", "prefetch-audio")
                   and not key.startswith(("cover-", "avatar-"))
                   for key in self.jobs):
                return

            def failed(error):
                source.retry_at = self.clock() + 15
                if self.queue_advance is not None or self.queue_flip is not None:
                    if self.queue_advance and not self.queue_advance[0]:
                        self.queue_advance = None
                    self.queue_flip = None
                    self.error(error)

            self.submit("queue-page", partial(self.client.tracks, source.playlist_id, source.offset),
                        lambda items: setattr(source, "fetched", items), failed)

    def load_playback_extras(self):
        player = self.player
        if player.status != "playing" or not player.current:
            return
        if any(key not in ("position", "control", "volume", "prefetch-audio")
               and not key.startswith(("cover-", "avatar-"))
               for key in self.jobs):
            return
        track_id = player.current["id"]
        if not self.background and self.tab == 2 and not self.help_visible and track_id not in self.lyric_cache:
            def done(raw):
                self.lyric_cache[track_id] = lyric_lines(raw)
                if len(self.lyric_cache) > 8:
                    del self.lyric_cache[next(iter(self.lyric_cache))]

            self.submit("lyric-preview", partial(self.client.lyrics, track_id), done,
                        lambda _error: self.lyric_cache.__setitem__(track_id, None))
            return
        if self.clock() - self.track_started_at < 12:
            return
        if "prefetch-audio" in self.jobs or not memory_available(72):
            return
        indices = list(self.next_indices())[:1] if player.mode != 2 else ()
        if indices and "prefetch-audio" not in self.jobs and self.clock() - self.track_started_at >= 3:
            following = player.upcoming[indices[0]]
            plan = (track_id, following["id"])
            if (self.audio_prefetch_attempt != plan and self.client.is_preloaded(following)
                    and not self.client.is_audio_prefetched(following)):
                self.audio_prefetch_attempt = plan
                self.submit("prefetch-audio", partial(self.client.prefetch_audio, following),
                            lambda _: None, lambda _error: None, download=True)
                return
        for index in indices:
            upcoming = player.upcoming[index]
            if upcoming["id"] not in self.preload_attempt and not self.client.is_preloaded(upcoming):
                self.preload_attempt.add(upcoming["id"])
                self.submit("preload", partial(self.client.preload, upcoming),
                            lambda _: None, lambda _error: None)
                return
    def advance(self, automatic=False, *, switch=False):
        if self.play_retry:
            if automatic:
                return
            target = self.play_retry.track
            self.play_retry = None
            self.player.upcoming = [track for track in self.player.upcoming if track is not target]
        index = next(iter(self.next_indices()), None)
        if index is None:
            _, source = self.queue_boundary()
            if source is not None:
                if not automatic:
                    source.retry_at = 0
                self.queue_advance = automatic, switch
                self.notice("正在加载后续曲目…", seconds=20)
                self.maintain_queue()
                return
            if not automatic:
                self.notice("没有下一首")
            return
        remaining = self.player.upcoming[:]
        track = remaining.pop(index)
        self.start_track(track, remaining, automatic=automatic, switch=switch)

    def previous_track(self):
        if not self.player.history:
            self.notice("没有上一首")
            return
        track = self.player.history[-1]
        upcoming = ([self.player.current] if self.player.current else []) + self.player.upcoming
        self.start_track(track, upcoming, previous=True)

    def toggle_playback(self):
        player = self.player
        if self.play_retry:
            self.play_retry.attempt = 0
            self.retry_playback()
            return
        if not player.current or "play" in self.jobs:
            return
        if player.status in ("ended", "failed", "resume"):
            position = player.position if player.status == "resume" else 0
            if player.duration and position >= player.duration - 2:
                position = 0
            self.start_track(player.current, player.upcoming[:], position=position)
        elif player.status in ("playing", "buffering"):
            self.submit("control", partial(self.client.control, "pause"),
                        lambda _: setattr(player, "status", "paused"))
        elif player.status == "paused":
            self.submit("control", partial(self.client.control, "resume"),
                        lambda _: setattr(player, "status", "playing"))

    def play_key(self, key):
        player = self.player
        if key in ("h", "help"):
            self.help_visible = not self.help_visible
        elif self.help_visible:
            return
        elif key in VOLUME:
            self.adjust_volume(VOLUME[key])
        elif self.pending_track:
            return
        elif key in ("6", "space"):
            self.toggle_playback()
        elif key == "5":
            self.previous_track()
        elif key == "7":
            self.advance()
        elif key == "4":
            player.mode = (player.mode + 1) % len(MODES)
            self.shuffle_next.clear()
        elif key == "8" and player.current:
            self.toggle_like(player.current)
        elif key == "enter" and player.current and player.status in ("failed", "resume"):
            self.toggle_playback()
        elif key == "enter" and player.current and player.status == "buffering":
            self.start_track(player.current, player.upcoming[:], position=player.position)

    def adjust_volume(self, step):
        player = self.player
        current = player.volume if self.requested_volume is None else self.requested_volume
        target = max(0, min(100, current + step))
        if self.pending_track or player.status in ("idle", "ended", "failed"):
            player.volume = target
            self.notice(f"音量 {target}%", seconds=1.8)
        else:
            self.requested_volume = target
            self.apply_volume()

    def apply_volume(self):
        target = self.requested_volume
        if target is None or "volume" in self.jobs:
            return

        def done(_result):
            self.player.volume = target
            self.notice(f"音量 {target}%", seconds=1.8)
            if self.requested_volume == target:
                self.requested_volume = None
            else:
                self.apply_volume()

        def failed(error):
            self.requested_volume = None
            self.error(error)

        self.submit("volume", partial(self.client.control, "volume", str(target)), done, failed)

    def toggle_like(self, track):
        if "like" in self.jobs:
            return
        liked = not track.get("liked", False)
        self.like_pending = (track["id"], self.clock())

        def done(_result):
            self.like_pending = None
            track["liked"] = liked
            self.notice("已收藏" if liked else "已取消收藏")

        def failed(error):
            self.like_pending = None
            self.error(error)

        self.submit("like", partial(self.client.like, track["id"], liked), done, failed)

    def queue_key(self, key):
        player = self.player
        offset = int(bool(player.current))
        boundary, source = self.queue_boundary()
        size = offset + boundary + int(source is not None)
        page_start = self.queue_selection // PLAYLIST_PAGE_SIZE * PLAYLIST_PAGE_SIZE
        if key == "z":
            self.queue_flip = None
            self.queue_selection = max(0, page_start - PLAYLIST_PAGE_SIZE)
            return
        if key == "c":
            target = page_start + PLAYLIST_PAGE_SIZE
            if source is not None and target >= offset + boundary:
                self.request_queue_page(source, target)
            elif target < size:
                self.queue_selection = target
            return
        if key in MOVES:
            self.queue_flip = None
        self.queue_selection = moved(self.queue_selection, key, size)
        if key == "space":
            self.toggle_playback()
        index = self.queue_selection - offset
        if not 0 <= index < len(player.upcoming) or "play" in self.jobs:
            return
        if key == "r":
            removed = player.upcoming.pop(index)
            if self.play_retry:
                if removed is self.play_retry.track:
                    self.play_retry = None
                    self.notice("已移除待重连曲目")
                else:
                    remaining = self.play_retry.remaining
                    for old_index, entry in enumerate(remaining):
                        if entry is removed:
                            remaining.pop(old_index)
                            break
            self.queue_flip = None
            self.queue_selection = max(0, min(size - 2, self.queue_selection))
        elif key == "enter":
            if source is not None and index == boundary:
                self.request_queue_page(source, self.queue_selection)
                return
            remaining = player.upcoming[:]
            track = remaining.pop(index)
            self.start_track(track, remaining, switch=True)

    def request_queue_page(self, source, target):
        source.retry_at = 0
        self.queue_flip = target
        self.notice("正在加载后续曲目…", seconds=20)
        self.maintain_queue()

    def view(self):
        notice = self.notice_text if self.clock() < self.notice_until else ""
        if self.screen != "ready" and not self.confirmation:
            return self.setup_view(notice)
        view = {"tab": self.tab, "notice": notice}
        if self.confirmation:
            title, _, selected = self.confirmation
            view.update(heading=title, rows=[("取消", ""), ("确认", "")], selected=selected,
                        footer="F/X 选择 · Enter 确认")
        elif self.tab == 0:
            view.update(self.search_view())
        elif self.tab == 1:
            view.update(self.list_view(self.library[-1]))
        elif self.tab == 2:
            view.update(self.player_view())
        else:
            view.update(self.queue_view())
        rows = view.get("rows") or []
        selected = view.get("selected", -1)
        scrollable = (not self.confirmation and (self.tab in (0, 3) or
                      self.tab == 1 and self.library[-1].kind in ("tracks", "playlists")))
        if scrollable and 0 <= selected < len(rows):
            context = id(self.library[-1]) if self.tab == 1 else self.search.generation if self.tab == 0 else 0
            key = self.tab, context, selected, rows[selected][0]
            if self.row_scroll_key != key:
                self.row_scroll_key, self.row_scroll_started = key, self.clock()
            view["row_scroll_elapsed"] = self.clock() - self.row_scroll_started
        else:
            self.row_scroll_key = None
        return view

    def setup_view(self, notice):
        if self.screen == "setup" and self.web:
            address = self.web.address()
            return dict(heading="在浏览器配置", detail=address or "先连接 Wi-Fi",
                        rows=[("访问码 " + self.web.code, "手机或电脑与 Zero 连接同一 Wi-Fi")],
                        footer="Esc 返回 · 长按退出", notice=notice)
        if self.screen == "need-setup":
            detail = "Enter 打开网页配置"
        elif self.screen == "checking":
            detail = "正在检查登录…"
        elif self.screen == "signed-out":
            detail = "Enter 扫码登录 · A 初始化"
        elif self.screen == "offline":
            detail = "网络连接失败，Enter 重试"
        else:
            detail = "用网易云音乐 App 扫码并确认" if self.qr else "Enter 获取登录二维码"
        return dict(heading="网易云音乐", detail=detail, qr=self.qr,
                    footer="Enter 刷新 · Esc 返回" if self.qr else "A 初始化 · 长按 Esc 退出" if self.screen == "login" else "长按 Esc 退出",
                    notice=notice)

    def search_view(self):
        search = self.search
        if search.editing:
            return dict(heading=search.query + "▏" if search.query else "输入歌名或歌手全拼",
                        detail="正在搜索…" if self.has_search_job() else "例如 qilixiang 或 linjunjie",
                        footer="Enter 搜索 · Tab 换页")
        tracks = self.visible_tracks(search.tracks)
        return dict(heading=search.query, rows=track_rows(tracks), selected=search.selected,
                    detail="" if tracks else "没有可播放的结果",
                    footer="F/X 选择 · Enter 播放 · A 入队" if tracks else "Esc 修改关键词")

    def list_view(self, page):
        if page.kind == "tracks":
            tracks = self.visible_tracks(page.items)
            rows = [("播放全部", "A 整单入队")] + track_rows(tracks) if tracks else []
            if page.has_more:
                rows.append(("加载更多", ""))
            detail = "" if tracks else "没有可播放的歌曲" if page.items else "暂无歌曲"
        elif page.kind == "playlists":
            rows = [(item.get("name") or "未命名歌单", "") for item in page.items]
            detail = "" if rows else "暂无歌单"
        else:
            settings = {"hide-unplayable": self.hide_unplayable, "background": self.background_playback}
            rows = [(label, ("开启" if settings[action] else "关闭") if action in settings else "")
                    for label, action in page.items]
            detail = ""
        footer = "Enter 打开 · A 入队 · Esc 返回" if page.kind in ("menu", "playlists") else \
            "Enter 播放 · A 入队 · Esc 返回" if page.kind == "tracks" else "F/X 选择 · Enter 打开 · Esc 返回"
        view = dict(heading=page.title, detail=detail, rows=rows, selected=page.selected, footer=footer)
        if page.kind == "menu":
            view.update(profile_name=self.account or "已登录", avatar=self.avatar)
        if self.library_loading and self.library_loading[0] is page:
            _, row, started = self.library_loading
            view.update(loading_row=row, loading_frame=int((self.clock() - started) * 6) % 8)
        return view

    def player_view(self):
        player = self.player
        if self.help_visible:
            return dict(heading="播放按键", rows=PLAYBACK_HELP, footer="短按 Esc 返回")
        track = self.pending_track or (self.play_retry.track if self.play_retry else player.current)
        title = (track.get("name") or "未知歌曲") if track else "还没有播放歌曲"
        progress = (0, 0) if self.pending_track and self.pending_track is not player.current else (
            player.position, player.duration)
        if self.play_retry and self.play_retry.track is not player.current:
            progress = self.play_retry.position, 0
        status = "starting" if self.pending_track else player.status
        if self.play_retry and not self.pending_track:
            status = "retrying" if self.play_retry.at is not None else "failed"
        info = {"status": status, "mode": MODES[player.mode], "volume": player.volume,
                "liked": bool(track and track.get("liked")), "artwork": self.artwork}
        preview = []
        if self.pending_track or status == "retrying":
            info["loading_frame"] = int(self.clock() * 6) % 8
        if track:
            if self.like_pending and self.like_pending[0] == track["id"]:
                info["like_frame"] = int((self.clock() - self.like_pending[1]) * 6) % 8
            lyrics = self.lyric_cache.get(track["id"], [])
            if lyrics and not self.pending_track and not self.play_retry:
                cursor = 0
                if lyrics[-1][0] > 0:
                    cursor = max((i for i, (start, _) in enumerate(lyrics) if start <= player.position), default=0)
                preview = [line for _, line in lyrics[cursor:cursor + 2]]
            elif not self.pending_track and not self.play_retry and player.status in ("playing", "paused", "buffering"):
                if track["id"] not in self.lyric_cache:
                    preview = ["歌词加载中…"]
                else:
                    preview = ["歌词加载失败" if lyrics is None else "暂无歌词"]
        return dict(heading=title, detail=artists(track) if track else "", progress=progress,
                    playback=info, lyric_preview=preview, title_scroll=self.clock() - self.title_started_at)

    def queue_view(self):
        player = self.player
        boundary, source = self.queue_boundary()
        entries = ([player.current] if player.current else []) + player.upcoming[:boundary]
        size = len(entries) + int(source is not None)
        selected = moved(self.queue_selection, "", size)
        start = selected // PLAYLIST_PAGE_SIZE * PLAYLIST_PAGE_SIZE
        stop = start + PLAYLIST_PAGE_SIZE
        rows = track_rows(entries[start:stop])
        if source is not None and start <= len(entries) < stop:
            rows.append((source.title, "正在加载…" if "queue-page" in self.jobs else "Enter 加载后续曲目"))
        if player.current and start == 0:
            rows[0] = (rows[0][0], "当前曲目 · " + rows[0][1])
        detail = f"第 {start // PLAYLIST_PAGE_SIZE + 1} 页" if rows else "队列为空"
        return dict(heading="播放队列", detail=detail, rows=rows, selected=selected - start,
                    footer="Z/C 翻页 · Enter 播放 · R 移除")

    def close(self, *, keep_playing=False):
        self.close_web()
        self.images.shutdown(wait=False, cancel_futures=True)
        self.client.cancel_pending()
        self.downloads.shutdown(wait=True, cancel_futures=True)
        self.audio.shutdown(wait=True, cancel_futures=True)
        self.pool.shutdown(wait=True, cancel_futures=True)
        if "initialize" in self.jobs or "logout" in self.jobs:
            self.player = Player()
            self.play_retry = None
        player = self.player
        if player.current or "play" in self.jobs:
            try:
                if "play" in self.jobs:
                    future = self.jobs["play"][0]
                    if self.pending_track and not future.cancelled() and future.exception() is None:
                        if future.result() is not False:
                            player.jump(self.pending_track, self.pending_queue)
                if player.status in ("starting", "playing", "paused", "buffering"):
                    ended = player.apply_status(self.client.state())
                    if ended and keep_playing:
                        self.queue_advance = True, False
            except NeteaseError:
                pass
            self.save_session(live=keep_playing)
            if not keep_playing:
                try:
                    self.client.control("stop")
                except NeteaseError:
                    pass


def moved(index, key, size):
    return max(0, min(size - 1, index + MOVES.get(key, 0)))


def save_queue_entry(entry):
    if isinstance(entry, PlaylistTail):
        return {"tail": {"title": entry.title, "playlist_id": entry.playlist_id, "offset": entry.offset,
                         "previous_ids": entry.previous_ids, "liked": entry.liked}}
    return entry


def restore_queue(entries):
    queue = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        if playable(entry):
            queue.append(entry)
            continue
        tail = entry.get("tail")
        if isinstance(tail, dict) and isinstance(tail.get("offset"), int) and tail["offset"] >= 0:
            ids = tail.get("previous_ids")
            if isinstance(ids, list) and tail.get("playlist_id") and isinstance(tail.get("title"), str):
                queue.append(PlaylistTail(tail["title"], tail["playlist_id"], tail["offset"], tuple(ids),
                                          tail.get("liked") is True))
    return queue
