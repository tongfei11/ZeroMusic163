"""Distinguish transport taps from continuous seeking on the playback page."""

from threading import Event
import unittest
from unittest.mock import patch

from app import App
from test_app import FakeClient, song


class PlaybackSeekTest(unittest.TestCase):
    def setUp(self):
        resource = patch("app.memory_available", return_value=True)
        resource.start()
        self.addCleanup(resource.stop)
        self.time = 0
        self.client = FakeClient()
        self.app = App(self.client, clock=lambda: self.time)
        self.addCleanup(self.app.close)
        self.finish("login-status")
        self.app.tab = 2
        self.app.player.current = song()
        self.app.player.status = "playing"
        self.app.player.position, self.app.player.duration = 40, 180
        self.app.player.upcoming = [song(id="next")]
        self.client.state = lambda: {"status": self.app.player.status,
                                     "position": self.app.player.position,
                                     "duration": self.app.player.duration}

    def finish(self, operation):
        if operation in self.app.jobs:
            self.app.jobs[operation][0].exception(timeout=2)
        self.app.tick()

    def hold(self, key):
        self.app.key(key, 1)
        self.time += 0.6
        self.app.tick()

    def test_long_next_seeks_and_release_does_not_change_song(self):
        self.hold("7")
        self.finish("seek")
        self.app.key("7", 0)
        self.assertEqual(self.app.player.current["id"], "one")
        self.assertEqual(self.app.player.upcoming[0]["id"], "next")
        self.assertEqual(self.client.plays, [])
        self.assertEqual(float(self.client.controls[-1][1]), 45)

    def test_rewind_clamps_at_zero(self):
        self.app.player.position = 2
        self.hold("5")
        self.finish("seek")
        self.app.key("5", 0)
        self.assertEqual(self.client.controls[-1][0], "seek")
        self.assertEqual(float(self.client.controls[-1][1]), 0)
        self.assertEqual(self.app.player.current["id"], "one")

    def test_forward_stops_before_end_and_keeps_paused_state(self):
        self.app.player.status = "paused"
        self.app.player.position = 179
        self.hold("7")
        self.finish("seek")
        self.app.key("7", 0)
        self.assertEqual(float(self.client.controls[-1][1]), 179.5)
        self.assertEqual(self.app.player.status, "paused")
        self.assertFalse(any(command[0] == "resume" for command in self.client.controls))

    def test_short_transport_taps_navigate_on_release(self):
        self.app.key("7", 1)
        self.assertNotIn("play", self.app.jobs)
        self.time += 0.1
        self.app.key("7", 0)
        self.finish("play")
        self.assertEqual(self.app.player.current["id"], "next")
        self.app.key("5", 1)
        self.time += 0.1
        self.app.key("5", 0)
        self.finish("play")
        self.assertEqual(self.app.player.current["id"], "one")

    def test_slow_seek_coalesces_pending_updates(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        original = self.client.control

        def delayed(command, *args):
            if command == "seek":
                entered.set()
                release.wait(2)
            return original(command, *args)

        self.client.control = delayed
        self.hold("7")
        self.assertTrue(entered.wait(1))
        self.time += 0.4
        self.app.tick()
        self.time += 0.4
        self.app.tick()
        self.app.key("7", 0)
        release.set()
        self.finish("seek")
        if "seek" in self.app.jobs:
            self.finish("seek")
        seeks = [args for command, *args in self.client.controls if command == "seek"]
        self.assertLessEqual(len(seeks), 2)
        self.assertGreater(float(seeks[-1][0]), float(seeks[0][0]))
        self.assertEqual(self.client.plays, [])
