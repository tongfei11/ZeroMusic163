"""Keyboard interaction and non-blocking CLI orchestration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from time import monotonic

from netease import NeteaseError
from player import MODES, Player, artists, lyric_lines, playable, track_rows
from setup_web import SetupWeb


TABS = ("搜索", "歌单", "播放", "队列")
LIBRARY = [("喜欢的音乐", "favorite"), ("我的歌单", "created"), ("每日推荐", "daily"), ("设置", "settings")]
SCENES = LIBRARY[:3]
SETTINGS = [("修改网页配置", "setup"), ("重新登录", "login"), ("退出登录", "logout")]
MOVES = {"f": -1, "up": -1, "x": 1, "down": 1}
PREVIOUS, NEXT = ("z", "left"), ("c", "right")


@dataclass
class Page:
    title: str
    kind: str
    items: list
    selected: int = 0


@dataclass
class Search:
    query: str = ""
    editing: bool = True
    generation: int = 0
    tracks: list = field(default_factory=list)
    selected: int = 0


class App:
    def __init__(self, client, web=SetupWeb, clock=monotonic):
        self.client, self.web_factory, self.clock = client, web, clock
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ncm")
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
        self.player = Player()
        self.track_started_at = 0
        self.queue_selection = 0
        self.lyrics_visible, self.lyric_cache = False, {}
        self.next_poll = 0
        self.notice_text, self.notice_until = "", 0
        self.retry = None
        self.start()

    def submit(self, key, action, success, failure=None):
        if key not in self.jobs:
            self.jobs[key] = (self.pool.submit(action), success, failure)

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
            self.account = ""
            self.request_login()
        if self.screen == "login" and self.qr and now >= self.qr_deadline:
            self.qr = ""
            self.notice("二维码已过期，按 Enter 刷新", seconds=300)
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
            self.submit("login-status", self.client.login_status, self.initial_login, self.login_error)
        else:
            self.open_setup()

    def initial_login(self, data):
        if not self.finish_login(data):
            self.request_login()

    def finish_login(self, data):
        if not data.get("success"):
            return False
        user = data.get("user") or data.get("userInfo") or data.get("profile") or data.get("data") or data
        if not isinstance(user, dict):
            user = {}
        self.account = user.get("nickname") or user.get("name") or "已登录"
        self.login_completed = True
        self.screen, self.qr = "ready", ""
        self.notice("")
        return True

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
        if key == "enter" and self.retry and self.retry[0] == self.tab:
            _, action = self.retry
            self.retry = None
            action()
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
            self.retry = None
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
        elif search.tracks:
            search.selected = moved(search.selected, key, len(search.tracks))
            self.track_key(key, search.tracks[search.selected])

    def search_edited(self):
        self.search.generation += 1
        self.retry = None
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
        if not page.items:
            return
        size = len(page.items) + (page.kind == "tracks")
        page.selected = moved(page.selected, key, size)
        if page.kind == "tracks":
            if page.selected == 0 and key == "enter":
                self.play_all(page.items)
            elif page.selected > 0:
                self.track_key(key, page.items[page.selected - 1])
            return
        if key == "space":
            self.toggle_playback()
        if key != "enter":
            return
        item = page.items[page.selected]
        if page.kind == "playlists":
            self.load_tracks(item.get("name") or "歌单", partial(self.client.tracks, item["id"]), page)
            return
        label, action = item
        if action == "settings":
            self.library.append(Page("设置", "settings", SETTINGS))
        elif action == "setup":
            self.open_setup()
        elif action == "login":
            self.confirmation = ["重新扫码登录？", self.do_logout, 0]
        elif action == "logout":
            self.confirmation = ["退出登录并停止播放？", self.do_logout, 0]
        elif action == "created":
            self.load_playlists(page)
        else:
            self.load_tracks(label, partial(self.fetch_tracks, action), page)

    def fetch_tracks(self, kind):
        items = self.client.library(kind)
        if kind == "favorite" and items:
            return [dict(track, liked=True) for track in self.client.tracks(items[0]["id"])]
        return items

    def load_playlists(self, page):
        def fetch():
            combined = self.client.library("created") + self.client.library("collected")
            return list({item["id"]: item for item in combined if item.get("id")}.values())

        self.load_list("我的歌单", "playlists", fetch, page)

    def load_tracks(self, title, fetch, page):
        self.load_list(title, "tracks", fetch, page)

    def load_list(self, title, kind, fetch, page):
        origin = self.tab

        def done(items):
            if self.library[-1] is page:
                self.library.append(Page(title, kind, items, selected=0))

        def failed(error):
            if self.library[-1] is page:
                self.error(error)
                self.retry = (origin, partial(self.load_list, title, kind, fetch, page))

        self.submit("library", fetch, done, failed)

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
        if "logout" in self.jobs:
            return
        origin, volume = self.tab, self.player.volume

        def play():
            self.client.play(track)
            try:
                self.client.control("resume")
                self.client.control("volume", str(volume))
            except NeteaseError as error:
                if getattr(error, "reauth", False):
                    raise
                # The daemon may still be opening the track. Observe its state below.
                return False
            return True

        def done(controls_applied):
            if previous:
                self.player.history.pop()
            self.player.jump(track, remaining, remember=not previous)
            self.track_started_at, self.next_poll = self.clock(), 0
            self.queue_selection = 0
            self.lyrics_visible = False
            self.notice("" if controls_applied else "正在等待播放器")
            if switch and self.tab == origin:
                self.tab = 2

        def failed(error):
            self.error(error)
            if automatic and not getattr(error, "reauth", False):
                self.player.upcoming = remaining
                self.advance()

        self.submit("play", play, done, failed)

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
        elif key == "space":
            self.toggle_playback()
        elif key in PREVIOUS:
            self.previous_track()
        elif key in NEXT:
            self.advance()
        elif key in MOVES and player.current:
            volume = max(0, min(100, player.volume - 5 * MOVES[key]))
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
            if kind == "created":
                self.load_playlists(self.library[-1])
            else:
                self.load_tracks(label, partial(self.fetch_tracks, kind), self.library[-1])

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
        return dict(heading=search.query, rows=track_rows(search.tracks), selected=search.selected,
                    footer="F/X 选择 · Enter 播放 · A 入队")

    def list_view(self, page):
        if page.kind == "tracks":
            rows = [("播放全部", "")] + track_rows(page.items) if page.items else []
            detail = "" if rows else "暂无歌曲"
        elif page.kind == "playlists":
            rows = [(item.get("name") or "未命名歌单", "") for item in page.items]
            detail = "" if rows else "暂无歌单"
        else:
            rows = [(item[0], "") for item in page.items]
            detail = self.account if page.kind == "settings" else ""
        if "library" in self.jobs:
            detail = "正在加载…"
        return dict(heading=page.title, detail=detail, rows=rows, selected=page.selected,
                    footer="F/X 选择 · Enter 打开 · Esc 返回")

    def player_view(self):
        player = self.player
        if self.overlay:
            return self.list_view(self.overlay)
        if not player.current:
            return dict(heading="还没有播放歌曲", footer="Tab 换页")
        title = player.current.get("name") or "未知歌曲"
        if self.lyrics_visible:
            lyrics = self.lyric_cache.get(player.current["id"])
            if not lyrics:
                detail = "正在加载…" if f"lyric-{player.current['id']}" in self.jobs else "暂无歌词"
                return dict(heading=title, detail=detail, footer="Esc 返回")
            active = max((i for i, (start, _) in enumerate(lyrics) if start <= player.position), default=0)
            if all(item[0] == 0 for item in lyrics):
                active = 0
            return dict(heading=title, rows=[(line, "") for _, line in lyrics],
                        selected=active, footer="Esc 返回")
        state = {"idle": "", "starting": "载入中", "playing": "播放中", "paused": "已暂停",
                 "ended": "已播完", "failed": "播放失败"}[player.status]
        time_text = f"{clock_label(player.position)} / {clock_label(player.duration)}" if player.duration else ""
        return dict(heading=title, detail=artists(player.current),
                    rows=[(f"{state}  ·  {MODES[player.mode]}  ·  音量 {player.volume}", time_text)],
                    progress=(player.position, player.duration),
                    footer="Z/C 切歌 · F/X 音量 · S 模式 · L 词 · H 收藏 · V 场景")

    def queue_view(self):
        player = self.player
        rows = track_rows(([player.current] if player.current else []) + player.upcoming)
        if player.current:
            rows[0] = (rows[0][0], "当前曲目 · " + rows[0][1])
        return dict(heading="播放队列", detail="" if rows else "队列为空", rows=rows,
                    selected=self.queue_selection, footer="F/X 选择 · Enter 播放 · R 移除")

    def close(self):
        self.close_web()
        self.client.cancel_pending()
        self.pool.shutdown(wait=True, cancel_futures=True)
        if self.player.current or "play" in self.jobs:
            try:
                self.client.control("stop")
            except NeteaseError:
                pass


def moved(index, key, size):
    return max(0, min(size - 1, index + MOVES.get(key, 0)))


def clock_label(value):
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"
