import unittest

from app import App
from player import Player, lyric_lines


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
        return {"success": True, "nickname": "测试"}

    def search(self, text):
        self.queries.append(text)
        return [song(), song("晴天", "two")]

    def library(self, kind):
        return [{"name": "喜欢的音乐", "id": "liked"}] if kind == "favorite" else [song()]

    def tracks(self, collection):
        assert collection == "liked"
        return [song()]

    def play(self, track):
        self.plays.append(track["id"])

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
        self.key("f")
        self.wait("control")
        self.assertEqual(self.app.player.volume, 80)
        self.key("c")
        self.wait("play")
        self.assertEqual(self.client.plays, ["one", "two"])
        self.assertEqual(self.app.player.upcoming, [])

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
