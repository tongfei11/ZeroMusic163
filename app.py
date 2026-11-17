"""Keyboard interaction and non-blocking CLI orchestration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic

from artwork import download_thumbnail

from netease import NeteaseError, PLAYLIST_PAGE_SIZE
from player import MODES, Player, artists, lyric_lines, playable, track_rows
from setup_web import SetupWeb


TABS = ("搜索", "歌单", "播放", "队列")
LIBRARY = [("喜欢的音乐", "favorite"), ("我的歌单", "created"), ("每日推荐", "daily"), ("设置", "settings")]
SCENES = LIBRARY[:3]
SETTINGS = [("隐藏不可播放曲目", "hide-unplayable"), ("修改网页配置", "setup"),
            ("重新登录", "login"), ("退出登录", "logout")]
MOVES = {"f": -1, "up": -1, "x": 1, "down": 1}
PREVIOUS, NEXT = ("z", "left"), ("c", "right")


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


class App:
    def __init__(self, client, web=SetupWeb, clock=monotonic, settings_path=None):
        self.client, self.web_factory, self.clock = client, web, clock
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ncm")
        self.images = ThreadPoolExecutor(max_workers=1, thread_name_prefix="images")
        self.audio = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio")
        self.settings_path = Path(settings_path) if settings_path else None
        self.hide_unplayable = False
        if self.settings_path:
            try:
                self.hide_unplayable = json.loads(self.settings_path.read_text()).get("hide_unplayable") is True
            except (OSError, ValueError, AttributeError):
                pass
        self.jobs = {}
        self.running = True
        self.screen = "checking"
        self.return_screen = "ready"
        self.tab, self.esc_started = 0, None
        self.search = Search()
        self.library = [Page("歌单", "menu", LIBRARY)]
        self.overlay = None
        self.confirmation = None
        self.web = None
        self.qr, self.qr_deadline = "", 0
        self.login_completed = False
        self.login_attempt = 0
        self.account = ""
        self.avatar_url, self.avatar = "", None
        self.profile_at = None
        self.artwork_url, self.artwork = "", None
        self.pending_track = None
        self.player = Player()
        self.track_started_at = 0
        self.queue_selection = 0
        self.lyrics_visible, self.lyric_cache = False, {}
        self.next_poll = 0
        self.notice_text, self.notice_until = "", 0
        self.start()

    def submit(self, key, action, success, failure=None, *, image=False):
        if key not in self.jobs:
            executor = self.images if image else self.audio if key in ("position", "control") else self.pool
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

        if self.screen == "setup" and self.web and self.web.saved.is_set():
            self.close_web()
            self.player = Player()
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
        if now < self.next_poll:
            return
        if self.screen == "login" and self.qr:
            self.next_poll = now + 2.5
            attempt = self.login_attempt
            self.submit("login-status", self.client.login_status,
                        lambda data: self.finish_login(data) if attempt == self.login_attempt else None,
                        lambda _error: None)
        if self.screen == "ready" and self.player.current and self.player.status in ("starting", "playing", "paused"):
            if "play" not in self.jobs and "control" not in self.jobs:
                self.next_poll = now + 0.9
                self.submit("position", self.client.state, self.position_changed, self.position_error)

    def notice(self, message, seconds=4):
        self.notice_text, self.notice_until = message, self.clock() + seconds

    def error(self, error):
        if getattr(error, "reauth", False) and self.screen == "ready":
            self.request_login()
        self.notice(str(error), seconds=7)

    def start(self):
        self.screen = "checking"
        if self.client.configured():
            self.submit("login-status", self.client.account_status, self.initial_login, self.login_error)
        else:
            self.open_setup()

    def initial_login(self, data):
        if not self.finish_login(data):
            self.request_login()

    def finish_login(self, data):
        if not data.get("success"):
            return False
        self.apply_account(data)
        self.profile_at = None if data.get("user") else self.clock() + 2
        self.login_completed = True
        self.screen, self.qr = "ready", ""
        self.notice("")
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

    def toggle_hidden_tracks(self):
        enabled = not self.hide_unplayable
        if self.settings_path:
            temporary = None
            try:
                with NamedTemporaryFile(mode="w", dir=self.settings_path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump({"hide_unplayable": enabled}, output)
                temporary.replace(self.settings_path)
            except OSError:
                self.notice("无法保存设置")
                return
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        self.hide_unplayable = enabled
        self.search.selected = 0

    def login_error(self, error):
        self.screen, self.qr = "login", ""
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
        self.login_attempt += 1
        self.qr = ""
        self.notice("")
        self.submit("login", self.client.login, self.show_qr, self.login_error)

    def show_qr(self, url):
        if self.screen == "login":
            self.qr, self.qr_deadline = url, self.clock() + 300
            self.next_poll = 0

    def key(self, key, value):
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
        if not value or (value == 2 and key not in (*MOVES, "backspace")):
            return
        if self.screen != "ready":
            if key == "enter" and self.screen == "need-setup":
                self.open_setup()
            elif key == "enter" and self.screen in ("login", "signed-out"):
                self.request_login()
            elif key == "a" and self.screen in ("login", "signed-out"):
                self.open_setup()
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
        if key == "tab":
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
        if self.screen == "setup":
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
            elif self.tab == 1 and len(self.library) > 1:
                self.library.pop()
            elif self.tab == 2:
                if self.overlay:
                    self.overlay = None
                else:
                    self.lyrics_visible = False

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
                if page.selected == 0 and key == "enter":
                    self.play_all(tracks)
                elif 0 < page.selected < rows:
                    self.track_key(key, tracks[page.selected - 1])
            return
        if not page.items:
            return
        page.selected = moved(page.selected, key, len(page.items))
        if key == "space":
            self.toggle_playback()
        if key != "enter":
            return
        item = page.items[page.selected]
        if page.kind == "playlists":
            self.open_list(partial(self.playlist_page, item.get("name") or "歌单", item["id"]), page)
        else:
            label, action = item
            if action == "settings":
                self.library.append(Page("设置", "settings", SETTINGS))
            elif action == "hide-unplayable":
                self.toggle_hidden_tracks()
            elif action == "setup":
                self.open_setup()
            elif action == "login":
                self.confirmation = ["重新扫码登录？", self.do_logout, 0]
            elif action == "logout":
                self.confirmation = ["退出登录并停止播放？", self.do_logout, 0]
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

    def open_list(self, fetch, parent):
        selected = parent.selected

        def still_selected():
            return self.library[-1] is parent and parent.selected == selected

        def done(page):
            if still_selected():
                self.library.append(page)
                self.notice("")

        def failed(error):
            if still_selected():
                self.error(error)

        self.submit("library", fetch, done, failed)

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

        self.submit("library", partial(self.client.tracks, page.playlist_id, offset), done, failed)

    def do_logout(self):
        self.screen = "checking"
        had_track = self.player.current is not None

        def logout():
            if had_track:
                self.client.control("stop")
            self.client.logout()

        def done(_result):
            self.player = Player()
            self.library = [Page("歌单", "menu", LIBRARY)]
            self.search = Search()
            self.tab, self.login_completed = 0, False
            self.clear_account()
            self.request_login()

        def failed(error):
            self.screen = "ready"
            self.error(error)

        self.submit("logout", logout, done, failed)

    def track_key(self, key, track):
        if key == "enter":
            self.start_track(track, self.player.upcoming[:], switch=True)
        elif key == "space":
            self.toggle_playback()
        elif key == "a" and "play" not in self.jobs:
            if not playable(track):
                self.notice("这首歌暂不可播放")
                return
            self.player.upcoming.append(track)
            self.notice("已加入队列")

    def play_all(self, tracks):
        playable_tracks = [track for track in tracks if playable(track)]
        if not playable_tracks:
            self.notice("没有可播放的歌曲")
            return
        action = partial(self.start_track, playable_tracks[0], playable_tracks[1:], switch=True)
        if self.player.upcoming:
            self.confirmation = ["替换当前队列？", action, 0]
        else:
            action()

    def start_track(self, track, remaining, *, switch=False, previous=False, automatic=False):
        if not playable(track):
            self.notice("这首歌暂不可播放")
            return
        if "logout" in self.jobs or "play" in self.jobs:
            return
        volume = self.player.volume
        self.pending_track = track
        self.lyrics_visible = False
        self.show_artwork(track)
        if switch:
            self.tab = 2

        def done(_result):
            self.pending_track = None
            if previous:
                self.player.history.pop()
            self.player.jump(track, remaining, remember=not previous)
            self.track_started_at, self.next_poll = self.clock(), 0
            self.queue_selection = 0
            self.lyrics_visible = False
            self.notice("")
            if self.player.volume != volume:
                self.submit("control", partial(self.client.control, "volume", str(self.player.volume)), lambda _: None)

        def failed(error):
            self.pending_track = None
            if self.player.current:
                self.show_artwork(self.player.current)
            else:
                self.player.current = track
                self.player.status = "failed"
            self.error(error)
            if automatic and not getattr(error, "reauth", False):
                self.player.upcoming = remaining
                self.advance()

        self.notice("")
        self.submit("play", partial(self.client.play, track, volume=volume), done, failed)

    def position_changed(self, state):
        if "play" in self.jobs or "control" in self.jobs:
            return
        if self.player.apply_status(state):
            if self.player.mode == 2:
                self.start_track(self.player.current, self.player.upcoming[:])
            else:
                self.advance(automatic=True)
        elif self.player.status == "starting" and self.clock() - self.track_started_at > 10:
            self.player.status = "failed"
            self.notice("载入失败 · Enter 重试 / C 下一首", seconds=8)
        elif self.player.status == "failed":
            self.notice("播放中断 · Enter 重试 / C 下一首", seconds=8)

    def position_error(self, error):
        if getattr(error, "reauth", False):
            self.error(error)
        elif self.player.status == "starting" and self.clock() - self.track_started_at > 10:
            self.player.status = "failed"
            self.error(error)

    def advance(self, automatic=False):
        index = self.player.next_index()
        if index is None:
            if not automatic:
                self.notice("没有下一首")
            return
        remaining = self.player.upcoming[:]
        track = remaining.pop(index)
        self.start_track(track, remaining, automatic=automatic)

    def previous_track(self):
        if not self.player.history:
            self.notice("没有上一首")
            return
        track = self.player.history[-1]
        upcoming = ([self.player.current] if self.player.current else []) + self.player.upcoming
        self.start_track(track, upcoming, previous=True)

    def toggle_playback(self):
        player = self.player
        if not player.current or "play" in self.jobs:
            return
        if player.status in ("ended", "failed"):
            self.start_track(player.current, player.upcoming[:])
        elif player.status == "playing":
            self.submit("control", partial(self.client.control, "pause"),
                        lambda _: setattr(player, "status", "paused"))
        elif player.status == "paused":
            self.submit("control", partial(self.client.control, "resume"),
                        lambda _: setattr(player, "status", "playing"))

    def play_key(self, key):
        player = self.player
        if self.overlay:
            self.scene_key(key)
        elif self.pending_track:
            if key in MOVES:
                player.volume = max(0, min(100, player.volume - 5 * MOVES[key]))
        elif key == "space":
            self.toggle_playback()
        elif key in PREVIOUS:
            self.previous_track()
        elif key in NEXT:
            self.advance()
        elif key in MOVES:
            volume = max(0, min(100, player.volume - 5 * MOVES[key]))
            if player.status in ("idle", "ended", "failed"):
                player.volume = volume
            else:
                self.submit("control", partial(self.client.control, "volume", str(volume)),
                            lambda _: setattr(player, "volume", volume))
        elif key == "s":
            player.mode = (player.mode + 1) % len(MODES)
        elif key == "l":
            self.open_lyrics()
        elif key == "h" and player.current:
            self.toggle_like(player.current)
        elif key == "v":
            self.overlay = Page("场景选择", "scene", SCENES)
        elif key == "enter" and player.current and player.status == "failed":
            self.start_track(player.current, player.upcoming[:])

    def open_lyrics(self):
        if not self.player.current:
            return
        self.lyrics_visible = not self.lyrics_visible
        track_id = self.player.current["id"]
        if self.lyrics_visible and track_id not in self.lyric_cache:
            def done(raw):
                self.lyric_cache[track_id] = lyric_lines(raw)

            self.submit(f"lyric-{track_id}", partial(self.client.lyrics, track_id), done)

    def toggle_like(self, track):
        liked = not track.get("liked", False)

        def done(_result):
            track["liked"] = liked
            self.notice("已收藏" if liked else "已取消收藏")

        self.submit("like", partial(self.client.like, track["id"], liked), done)

    def scene_key(self, key):
        page = self.overlay
        page.selected = moved(page.selected, key, len(page.items))
        if key == "enter":
            label, kind = page.items[page.selected]
            self.overlay = None
            self.tab = 1
            self.library = [Page("歌单", "menu", LIBRARY)]
            self.open_list(partial(self.collection_page, label, kind), self.library[-1])

    def queue_key(self, key):
        player = self.player
        offset = int(bool(player.current))
        size = offset + len(player.upcoming)
        self.queue_selection = moved(self.queue_selection, key, size)
        if key == "space":
            self.toggle_playback()
        index = self.queue_selection - offset
        if not 0 <= index < len(player.upcoming) or "play" in self.jobs:
            return
        if key == "r":
            player.upcoming.pop(index)
            self.queue_selection = max(0, min(size - 2, self.queue_selection))
        elif key == "enter":
            remaining = player.upcoming[:]
            track = remaining.pop(index)
            self.start_track(track, remaining, switch=True)

    def view(self):
        notice = self.notice_text if self.clock() < self.notice_until else ""
        if self.screen != "ready":
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
            detail = "Enter 扫码登录 · A 修改配置"
        else:
            detail = "用网易云音乐 App 扫码并确认" if self.qr else "Enter 获取登录二维码"
        return dict(heading="网易云音乐", detail=detail, qr=self.qr,
                    footer="Enter 刷新 · Esc 返回" if self.qr else "A 修改配置 · 长按 Esc 退出" if self.screen == "login" else "长按 Esc 退出",
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
            play_label = "播放已加载曲目" if page.has_more else "播放全部"
            tracks = self.visible_tracks(page.items)
            rows = [(play_label, "")] + track_rows(tracks) if tracks else []
            if page.has_more:
                rows.append(("加载更多", ""))
            detail = "" if tracks else "没有可播放的歌曲" if page.items else "暂无歌曲"
        elif page.kind == "playlists":
            rows = [(item.get("name") or "未命名歌单", "") for item in page.items]
            detail = "" if rows else "暂无歌单"
        else:
            rows = [(label, ("开启" if self.hide_unplayable else "关闭") if action == "hide-unplayable" else "")
                    for label, action in page.items]
            detail = self.account if page.kind == "settings" else ""
        if "library" in self.jobs:
            detail = "正在加载…"
        view = dict(heading=page.title, detail=detail, rows=rows, selected=page.selected,
                    footer="F/X 选择 · Enter 打开 · Esc 返回")
        if page.kind == "menu":
            view.update(profile_name=self.account or "已登录", avatar=self.avatar)
        return view

    def player_view(self):
        player = self.player
        if self.overlay:
            return self.list_view(self.overlay)
        track = self.pending_track or player.current
        if not track:
            return dict(heading="还没有播放歌曲", detail=f"音量 {player.volume}", footer="F/X 音量 · Tab 换页")
        title = track.get("name") or "未知歌曲"
        if self.lyrics_visible and not self.pending_track:
            lyrics = self.lyric_cache.get(player.current["id"])
            if not lyrics:
                detail = "正在加载…" if f"lyric-{player.current['id']}" in self.jobs else "暂无歌词"
                return dict(heading=title, detail=detail, footer="Esc 返回")
            active = max((i for i, (start, _) in enumerate(lyrics) if start <= player.position), default=0)
            if all(item[0] == 0 for item in lyrics):
                active = 0
            return dict(heading=title, rows=[(line, "") for _, line in lyrics],
                        selected=active, footer="Esc 返回")
        progress = (0, 0) if self.pending_track else (player.position, player.duration)
        status = "starting" if self.pending_track else player.status
        return dict(heading=title, detail=artists(track), progress=progress,
                    playback={"status": status, "mode": MODES[player.mode], "volume": player.volume,
                              "liked": bool(track.get("liked")), "artwork": self.artwork})

    def queue_view(self):
        player = self.player
        rows = track_rows(([player.current] if player.current else []) + player.upcoming)
        if player.current:
            rows[0] = (rows[0][0], "当前曲目 · " + rows[0][1])
        return dict(heading="播放队列", detail="" if rows else "队列为空", rows=rows,
                    selected=self.queue_selection, footer="F/X 选择 · Enter 播放 · R 移除")

    def close(self):
        self.close_web()
        self.images.shutdown(wait=False, cancel_futures=True)
        self.audio.shutdown(wait=True, cancel_futures=True)
        self.client.cancel_pending()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.player.current or "play" in self.jobs:
            try:
                self.client.control("stop")
            except NeteaseError:
                pass


def moved(index, key, size):
    return max(0, min(size - 1, index + MOVES.get(key, 0)))
