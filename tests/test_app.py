from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from app import App
from player import Player, lyric_lines
from netease import NeteaseError, PLAYLIST_PAGE_SIZE


def song(name="七里香", id="one"):
    return {"id": id, "originalId": "123", "name": name, "artists": ["歌手"], "playFlag": True}


class FakeClient:
    def __init__(self):
        self.controls = []
        self.plays = []
        self.queries = []

    def configured(self):
        return True

    def login_status(self):
        return {"success": True, "user": {"nickname": "测试帐号"}}

    account_status = login_status

    def search(self, text):
        self.queries.append(text)
        return [song(), song("晴天", "two")]

    def library(self, kind):
        return [{"name": "喜欢的音乐", "id": "liked"}] if kind == "favorite" else [song()]

    def tracks(self, collection, offset=0):
        assert collection == "liked"
        return [song()]

    def play(self, track, *, volume):
        self.plays.append(track["id"])

    def preload(self, _track):
        pass

    def is_preloaded(self, _track):
        return False

    def lyrics(self, _track_id):
        return ""

    def control(self, command, *args):
        self.controls.append((command, *args))

    def cancel_pending(self):
        pass

    def state(self):
        return {"status": "playing", "position": 4, "duration": 195}


class AppTest(unittest.TestCase):
    def setUp(self):
        self.time = 0
        self.client = FakeClient()
        self.app = App(self.client, clock=lambda: self.time)
        self.addCleanup(self.app.close)
        self.wait("login-status")

    def wait(self, operation):
        self.app.jobs[operation][0].result(timeout=2)
        self.app.tick()

    def key(self, key):
        self.app.key(key, 1)
        self.app.key(key, 0)

    def test_tab_switch_is_direct_and_preserves_unsubmitted_search(self):
        for key in "qili":
            self.key(key)
        for target in (1, 2, 3, 0):
            self.key("tab")
            self.assertEqual(self.app.view()["tab"], target)
        self.assertEqual(self.app.search.query, "qili")
        self.assertTrue(self.app.search.editing)

    def test_pinyin_play_queue_and_volume(self):
        for key in "qilixiang":
            self.key(key)
        self.key("enter")
        self.wait(f"search-{self.app.search.generation}")
        self.assertEqual(self.client.queries, ["qilixiang"])
        self.key("x")
        self.key("a")
        self.key("f")
        self.key("enter")
        self.wait("play")
        self.assertEqual(self.app.tab, 2)
        self.assertEqual(self.client.plays, ["one"])
        self.assertEqual([x["id"] for x in self.app.player.upcoming], ["two"])
        self.wait("position")
        self.key("d")
        self.wait("volume")
        self.assertEqual(self.app.player.volume, 80)
        self.key("7")
        self.wait("play")
        self.assertEqual(self.client.plays, ["one", "two"])
        self.assertEqual(self.app.player.upcoming, [])

    def test_random_mode_prefetches_the_same_track_next_uses(self):
        first = song()
        self.app.player.current = first
        self.app.player.upcoming = [song("晴天", "two"), song("稻香", "three")]
        self.app.player.status = "playing"
        self.key("tab")
        self.key("tab")
        self.key("4")
        preloaded = []
        self.client.preload = lambda track: preloaded.append(track["id"])
        self.time = 3
        self.app.tick()
        self.wait("preload")
        self.assertEqual(len(preloaded), 1)
        self.key("7")
        self.wait("play")
        self.assertEqual(self.client.plays[-1], preloaded[0])

    def test_cached_next_does_not_wait_for_lyrics_cli(self):
        self.app.player.current = song()
        self.app.player.upcoming = [song("晴天", "two")]
        self.app.player.status = "playing"
        self.key("tab")
        self.key("tab")
        release = Event()
        self.addCleanup(release.set)
        self.app.submit("lyric-preview", lambda: release.wait(2), lambda _: None)
        self.client.is_preloaded = lambda track: track["id"] == "two"
        self.client.play_cached = lambda track, volume: self.client.plays.append(track["id"]) or True
        self.key("7")
        self.wait("play")
        self.assertEqual(self.app.player.current["id"], "two")
        self.assertIn("lyric-preview", self.app.jobs)
        release.set()

    def test_playback_help_and_like_waiting_are_separate_keys(self):
        self.app.player.current = song()
        self.app.player.status = "playing"
        self.key("tab")
        self.key("tab")
        self.key("h")
        self.assertEqual(self.app.view()["heading"], "播放按键")
        self.key("esc")
        self.assertIn("playback", self.app.view())
        released = Event()
        self.addCleanup(released.set)
        self.client.like = lambda _id, _liked: released.wait(2)
        self.key("8")
        self.assertIn("like_frame", self.app.view()["playback"])
        self.assertFalse(self.app.view()["playback"]["liked"])
        released.set()
        self.wait("like")
        self.assertNotIn("like_frame", self.app.view()["playback"])
        self.assertTrue(self.app.view()["playback"]["liked"])

    def test_lyric_preview_follows_playback_position(self):
        self.app.player.current = song()
        self.app.player.position = 4
        self.app.lyric_cache["one"] = [(1, "第一行"), (3, "第二行"), (7, "第三行")]
        self.key("tab")
        self.key("tab")
        self.assertEqual(self.app.view()["lyric_preview"], ["第二行", "第三行"])

    def test_enter_shows_loading_playback_before_worker_result(self):
        for key in "qilixiang":
            self.key(key)
        self.key("enter")
        self.wait(f"search-{self.app.search.generation}")
        self.key("enter")
        self.assertEqual(self.app.tab, 2)
        self.assertEqual(self.app.view()["heading"], "七里香")
        self.assertEqual(self.app.view()["playback"]["status"], "starting")
        self.assertIsNone(self.app.player.current)
        self.key("tab")
        self.wait("play")
        self.assertEqual(self.app.tab, 3)
        self.assertEqual(self.app.player.current["id"], "one")

    def test_hide_unplayable_is_saved_and_pagination_uses_raw_offset(self):
        with TemporaryDirectory() as temporary:
            self.app.settings_path = Path(temporary) / "settings.json"
            self.key("tab")
            self.assertEqual(self.app.view()["profile_name"], "测试帐号")
            for _ in range(3):
                self.key("x")
            self.key("enter")
            self.key("enter")
            self.assertTrue(self.app.hide_unplayable)
            self.assertIn(("隐藏不可播放曲目", "开启"), self.app.view()["rows"])
            saved = App(self.client, settings_path=self.app.settings_path)
            self.addCleanup(saved.close)
            self.assertTrue(saved.hide_unplayable)
            self.key("esc")
            for _ in range(3):
                self.key("f")
            offsets = []

            def paged(_collection, offset=0):
                offsets.append(offset)
                if offset:
                    return [song(id="playable")]
                return [dict(song(id=str(index)), playFlag=False) for index in range(PLAYLIST_PAGE_SIZE)]

            self.client.tracks = paged
            self.key("enter")
            self.wait("library")
            self.assertEqual(self.app.view()["rows"], [("加载更多", "")])
            self.key("enter")
            self.wait("library")
            self.assertEqual(offsets, [0, PLAYLIST_PAGE_SIZE])
            self.assertEqual(self.app.view()["rows"][-1][0], "七里香")

    def test_volume_can_be_set_before_playback(self):
        self.key("tab")
        self.key("tab")
        self.key("s")
        self.key("volume-down")
        self.assertEqual(self.app.player.volume, 65)
        self.assertFalse(self.client.controls)
        view = self.app.view()
        self.assertEqual(view["playback"]["volume"], 65)
        self.assertEqual(view["playback"]["status"], "idle")
        self.assertEqual(view["progress"], (0, 0))

    def test_close_saves_position_and_reopen_resumes_on_play(self):
        with TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            first = App(self.client, settings_path=settings)
            first.player.current = song()
            first.player.upcoming = [song(id="next")]
            first.player.status = "playing"
            self.client.state = lambda: {"status": "playing", "position": 47.5, "duration": 180}
            first.close()
            self.assertIn(("stop",), self.client.controls)

            reopened = App(self.client, settings_path=settings)
            try:
                reopened.jobs["login-status"][0].result(timeout=2)
                reopened.tick()
                self.assertEqual(reopened.view()["progress"], (47.5, 180))
                self.assertEqual(self.client.plays, [])
                self.client.play = lambda track, **options: self.client.plays.append((track["id"], options))
                reopened.key("6", 1)
                reopened.jobs["play"][0].result(timeout=2)
                reopened.tick()
                self.assertEqual(self.client.plays, [("one", {"volume": 75, "position": 47.5})])
                self.assertEqual(reopened.player.upcoming[0]["id"], "next")
            finally:
                reopened.close()

    def test_background_setting_persists_and_worker_continues_queue(self):
        with TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            first = App(self.client, settings_path=settings)
            first.toggle_setting("background_playback")
            first.player.current = song()
            first.player.upcoming = [song(id="next")]
            first.player.status = "playing"
            first.close(keep_playing=True)
            self.assertNotIn(("stop",), self.client.controls)

            self.client.adopt_playback = lambda: {"status": "stopped"}
            worker = App(self.client, settings_path=settings, background=True)
            try:
                self.assertTrue(worker.background_playback)
                worker.jobs["play"][0].result(timeout=2)
                worker.tick()
                self.assertEqual(self.client.plays, ["next"])
                self.assertFalse(worker.background_finished)
            finally:
                worker.close()

    def test_escaped_search_does_not_show_stale_result(self):
        self.key("x")
        self.key("enter")
        old_job = f"search-{self.app.search.generation}"
        self.key("esc")
        self.wait(old_job)
        self.assertTrue(self.app.search.editing)
        self.assertFalse(self.app.search.tracks)
        self.assertEqual(self.app.search.query, "x")

    def test_favorite_resolves_playlist_to_tracks(self):
        self.key("tab")
        self.key("enter")
        self.wait("library")
        page = self.app.library[-1]
        self.assertEqual(page.kind, "tracks")
        self.assertTrue(page.items[0]["liked"])
        self.key("esc")
        self.assertEqual(len(self.app.library), 1)

    def test_library_loading_animates_on_the_requested_row(self):
        released = Event()
        self.addCleanup(released.set)
        original = self.client.library

        def blocked(kind):
            released.wait(2)
            return original(kind)

        self.client.library = blocked
        self.key("tab")
        self.key("enter")
        first = self.app.view()
        self.assertEqual(first["profile_name"], "测试帐号")
        self.assertEqual(first["loading_row"], 0)
        self.time += 0.3
        self.key("x")
        second = self.app.view()
        self.assertEqual(second["selected"], 1)
        self.assertEqual(second["loading_row"], 0)
        self.assertNotEqual(second["loading_frame"], first["loading_frame"])
        released.set()
        self.wait("library")
        self.assertNotIn("loading_row", self.app.view())

    def test_next_page_failure_keeps_existing_tracks(self):
        seen = []

        def paged(_collection, offset=0):
            seen.append(offset)
            if offset:
                raise NeteaseError("连接网易云音乐失败")
            return [song(id=str(index)) for index in range(PLAYLIST_PAGE_SIZE)]

        self.client.tracks = paged
        self.key("tab")
        self.key("enter")
        self.wait("library")
        page = self.app.library[-1]
        self.assertEqual(seen, [0])
        self.assertEqual(self.app.view()["rows"][-1][0], "加载更多")
        page.selected = len(page.items) + 1
        self.key("enter")
        with self.assertRaises(NeteaseError):
            self.app.jobs["library"][0].result(timeout=2)
        self.app.tick()
        self.assertEqual(seen, [0, PLAYLIST_PAGE_SIZE])
        self.assertEqual(len(page.items), PLAYLIST_PAGE_SIZE)
        self.client.tracks = lambda _collection, _offset: [song(id="more")]
        self.key("enter")
        self.wait("library")
        self.assertEqual(len(page.items), PLAYLIST_PAGE_SIZE + 1)
        self.assertEqual(self.app.view()["rows"][0][0], "播放全部")

    def test_only_last_confirmed_library_request_opens(self):
        started, released = Event(), Event()
        self.addCleanup(released.set)
        requested = []
        original = self.client.library

        def blocked(kind):
            requested.append(kind)
            if kind == "favorite":
                started.set()
                released.wait(2)
            return original(kind)

        self.client.library = blocked
        self.key("tab")
        self.key("enter")
        self.assertTrue(started.wait(2))
        self.key("x")
        self.key("enter")
        self.key("x")
        self.key("enter")
        self.assertEqual(self.app.view()["loading_row"], 2)
        self.assertEqual(requested, ["favorite"])
        released.set()
        self.wait("library")
        self.assertEqual(len(self.app.library), 1)
        self.wait("library")
        self.assertEqual(requested, ["favorite", "daily"])
        self.assertEqual(self.app.library[-1].title, "每日推荐")

    def test_daily_recommendations_can_be_queued_together(self):
        self.client.library = lambda _kind: [
            song("第一首", "first"), dict(song("灰歌", "blocked"), playFlag=False), song("第三首", "third")
        ]
        for key in ("tab", "x", "x", "a"):
            self.key(key)
        self.wait("library")
        self.key("tab")
        self.key("tab")
        self.assertEqual([title for title, _ in self.app.view()["rows"]], ["第一首", "第三首"])
        self.assertEqual(self.client.plays, [])

    def test_favorite_queue_can_request_a_later_page(self):
        offsets = []

        def paged(_collection, offset=0):
            offsets.append(offset)
            end = PLAYLIST_PAGE_SIZE if not offset else offset + 2
            return [song(f"第 {index + 1} 首", str(index)) for index in range(offset, end)]

        self.client.tracks = paged
        self.key("tab")
        self.key("a")
        self.wait("library")
        self.key("tab")
        self.key("tab")
        self.assertEqual(offsets, [0])
        self.assertEqual(self.app.view()["rows"][0][0], "第 1 首")
        self.key("c")
        self.wait("queue-page")
        self.assertEqual(offsets, [0, PLAYLIST_PAGE_SIZE])
        self.assertEqual(self.app.view()["rows"][0][0], "第 31 首")
        self.key("z")
        self.assertEqual(self.app.view()["rows"][0][0], "第 1 首")

    def test_failed_entry_does_not_override_next_selection(self):
        initial = self.client.library

        def fail_favorite(kind):
            if kind == "favorite":
                raise NeteaseError("连接网易云音乐失败")
            return initial(kind)

        self.client.library = fail_favorite
        self.key("tab")
        self.key("enter")
        with self.assertRaises(NeteaseError):
            self.app.jobs["library"][0].result(timeout=2)
        self.app.tick()
        self.key("x")
        self.key("x")
        self.key("enter")
        self.wait("library")
        self.assertEqual(self.app.library[-1].title, "每日推荐")

    def test_escape_hold_exits_only_at_threshold(self):
        self.key("esc")
        self.assertTrue(self.app.running)
        self.app.key("esc", 1)
        self.time += 2.99
        self.app.tick()
        self.assertTrue(self.app.running)
        self.app.key("esc", 2)
        self.time += 0.02
        self.app.tick()
        self.assertFalse(self.app.running)


class PlayerTest(unittest.TestCase):
    def test_end_and_interruption_are_distinct(self):
        player = Player()
        player.jump(song(), [])
        self.assertFalse(player.apply_status({"status": "playing", "position": 190, "duration": 192}))
        self.assertTrue(player.apply_status({"status": "stopped", "position": 0}))
        self.assertEqual(player.status, "ended")
        player.jump(song(), [])
        player.apply_status({"status": "playing", "position": 5, "duration": 192})
        self.assertFalse(player.apply_status({"status": "stopped", "position": 0}))
        self.assertEqual(player.status, "failed")

    def test_lrc_repeated_timestamps(self):
        self.assertEqual(lyric_lines("[00:03.0][00:07.5]歌词"), [(3, "歌词"), (7.5, "歌词")])
