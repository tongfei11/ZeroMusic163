"""Recovery behavior with unavailable networks and preserved queue state."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from app import App, Page, SETTINGS
from netease import Netease, NeteaseError
from test_app import FakeClient, song


class NetworkPlaybackTest(unittest.TestCase):
    def setUp(self):
        resource = patch("app.memory_available", return_value=True)
        resource.start()
        self.addCleanup(resource.stop)
        self.time = 0
        self.client = FakeClient()
        self.app = App(self.client, clock=lambda: self.time)
        self.addCleanup(self.app.close)
        self.finish("login-status")

    def finish(self, operation):
        # Await both successful and failed workers so App.tick handles the result.
        self.app.jobs[operation][0].exception(timeout=2)
        self.app.tick()

    def fail_play(self, *_args, **_kwargs):
        raise NeteaseError("网络连接失败", retryable=True)

    def test_network_failure_keeps_next_song_and_later_queue_until_retry_succeeds(self):
        current, target, later = song(), song(id="next"), song(id="later")
        self.app.player.current = current
        self.app.player.status = "ended"
        self.app.player.upcoming = [target, later]
        self.client.play = self.fail_play
        self.app.advance(automatic=True)
        self.finish("play")
        self.assertIs(self.app.player.current, current)
        self.assertEqual(self.app.player.upcoming, [target, later])
        self.assertIs(self.app.play_retry.track, target)

        self.client.play = lambda track, **_options: self.client.plays.append(track["id"])
        self.time = self.app.play_retry.at
        self.app.tick()
        self.finish("play")
        self.assertIsNone(self.app.play_retry)
        self.assertIs(self.app.player.current, target)
        self.assertEqual(self.app.player.upcoming, [later])
        self.assertEqual(self.client.plays, ["next"])

    def test_repeated_network_failure_stops_automatic_retries(self):
        self.client.play = self.fail_play
        self.app.start_track(song(), [])
        self.finish("play")
        for _ in range(5):
            self.assertIsNotNone(self.app.play_retry.at)
            self.time = self.app.play_retry.at
            self.app.tick()
            self.finish("play")
        self.assertIsNone(self.app.play_retry.at)
        self.time += 300
        self.app.tick()
        self.assertNotIn("play", self.app.jobs)
        self.assertIn("6 重试", self.app.notice_text)

    def test_rate_limit_waits_before_retry_and_keeps_the_target(self):
        error = Netease._error({"code": 429, "message": "服务忙"})
        self.assertTrue(error.retryable)

        def limited(*_args, **_kwargs):
            raise error

        self.client.play = limited
        target = song()
        self.app.start_track(target, [])
        self.finish("play")
        self.assertGreaterEqual(self.app.play_retry.at - self.time, 30)
        self.assertIs(self.app.play_retry.track, target)

    def test_auth_quota_and_song_restriction_are_not_network_errors(self):
        auth = Netease._error(None, "HTTP 401: Unauthorized")
        self.assertTrue(auth.reauth)
        self.assertFalse(auth.retryable)
        rights = Netease._error({"message": "歌曲已下架"})
        self.assertTrue(rights.unavailable)
        self.assertFalse(rights.retryable)
        quota = Netease._error({"message": "请求总量超限"})
        self.assertFalse(quota.retryable)
        self.assertFalse(quota.reauth)
        server = Netease._error({"code": 503, "message": "繁忙"})
        self.assertTrue(server.retryable)

    def test_queue_edits_during_recovery_survive_successful_retry(self):
        target, later, added = song(id="next"), song(id="later"), song(id="added")
        self.app.player.current = song()
        self.app.player.status = "ended"
        self.app.player.upcoming = [target, later]
        self.client.play = self.fail_play
        self.app.advance(automatic=True)
        self.finish("play")
        self.app.track_key("a", added)
        self.app.queue_selection = 2
        self.app.queue_key("r")
        self.client.play = lambda *_args, **_kwargs: None
        self.time = self.app.play_retry.at
        self.app.tick()
        self.finish("play")
        self.assertEqual(self.app.player.upcoming, [added])

    def test_initialization_waits_for_cached_play_before_reset(self):
        entered, release, reset = Event(), Event(), Event()
        self.addCleanup(release.set)
        calls = []

        def cached(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            calls.append("play")
            return True

        def clear():
            calls.append("reset")
            reset.set()

        self.client.is_preloaded = lambda _: True
        self.client.play_cached = cached
        self.client.reset = clear
        self.client.configure = lambda *_: None
        self.app.web_factory = lambda *_args, **_kwargs: type("Web", (), {
            "saved": Event(), "close": lambda _: None})()
        self.app.start_track(song(), [])
        self.assertTrue(entered.wait(1))
        self.app.initialize()
        self.assertFalse(reset.wait(0.05))
        release.set()
        self.finish("initialize")
        self.assertEqual(calls, ["play", "reset"])
        self.assertIsNone(self.app.player.current)

    def test_buffering_preserves_progress_and_pause_still_works(self):
        self.app.player.current = song()
        self.app.player.status = "playing"
        self.app.player.position, self.app.player.duration = 42, 180
        self.app.position_changed({"status": "buffering", "position": None, "duration": None})
        self.assertEqual((self.app.player.position, self.app.player.duration), (42, 180))
        self.assertEqual(self.app.player.status, "buffering")
        self.app.toggle_playback()
        self.finish("control")
        self.assertIn(("pause",), self.client.controls)
        self.assertEqual(self.app.player.status, "paused")

    def test_starting_offline_shows_retry_screen_without_starting_login(self):
        self.client.account_status = lambda: self.fail_play()
        app = App(self.client, clock=lambda: self.time)
        self.addCleanup(app.close)
        app.jobs["login-status"][0].exception(timeout=2)
        app.tick()
        self.assertEqual(app.screen, "offline")
        self.assertNotIn("login", app.jobs)
        self.assertFalse(app.qr)

    def test_settings_have_background_second_and_no_username(self):
        self.assertEqual([action for _, action in SETTINGS],
                         ["hide-unplayable", "background", "setup", "login"])
        view = self.app.list_view(Page("设置", "settings", SETTINGS))
        self.assertEqual(view["detail"], "")
        self.assertNotIn("profile_name", view)
        self.assertEqual([label for label, _ in SETTINGS],
                         ["隐藏不可播放曲目", "后台播放", "初始化", "切换账号"])

    def test_initialize_clears_user_settings_then_opens_first_configuration(self):
        class Web:
            def __init__(self, *_args, **_kwargs):
                self.saved = Event()

            def close(self):
                pass

        calls = []
        self.client.reset = lambda: calls.append("reset")
        self.client.configure = lambda *_args: None
        self.app.web_factory = Web
        self.app.player.current = song()
        self.app.background_playback = self.app.hide_unplayable = True
        self.app.initialize()
        self.finish("initialize")
        self.assertEqual(calls, ["reset"])
        self.assertEqual(self.app.screen, "setup")
        self.assertIsNone(self.app.player.current)
        self.assertFalse(self.app.background_playback)
        self.assertFalse(self.app.hide_unplayable)
        self.assertEqual(self.app.account, "")

    def test_pending_network_recovery_survives_background_handoff(self):
        with TemporaryDirectory() as directory:
            self.app.settings_path = Path(directory) / "settings.json"
            self.app.resume_path = Path(directory) / "playback.json"
            current, next_track = song(), song(id="next")
            self.app.player.current = current
            self.app.player.upcoming = [next_track]
            self.app.player.status = "ended"
            self.app.schedule_playback_retry(next_track, [], automatic=True)
            self.app.save_session(live=True)
            self.client.adopt_playback = lambda: {"status": "stopped"}
            restored = App(self.client, clock=lambda: self.time,
                           settings_path=self.app.settings_path, background=True)
            self.addCleanup(restored.close)
            self.assertEqual(restored.play_retry.track["id"], "next")
            self.assertTrue(restored.should_keep_playing)
            self.time = restored.play_retry.at
            restored.tick()
            restored.jobs["play"][0].exception(timeout=2)
            restored.tick()
            self.assertEqual(restored.player.current["id"], "next")

    def test_change_account_clears_previous_user_and_queue_before_qr(self):
        self.app.player.current = song()
        self.app.player.upcoming = [song(id="next")]
        self.client.logout = lambda: None
        self.client.login = lambda: "https://163cn.tv/mock"
        self.app.change_account()
        self.finish("logout")
        self.finish("login")
        self.assertEqual(self.app.screen, "login")
        self.assertIsNone(self.app.player.current)
        self.assertEqual(self.app.player.upcoming, [])
        self.assertEqual(self.app.account, "")
        self.assertEqual(self.app.qr, "https://163cn.tv/mock")
        self.assertIn(("stop",), self.client.controls)

    def test_selected_row_scroll_restarts_after_moving_selection(self):
        self.app.search.editing = False
        self.app.search.tracks = [song("很长的歌名" * 8), song("另一首很长的歌名" * 8, id="next")]
        self.assertEqual(self.app.view()["row_scroll_elapsed"], 0)
        self.time = 2
        self.assertEqual(self.app.view()["row_scroll_elapsed"], 2)
        self.app.key("x", 1)
        self.assertEqual(self.app.view()["row_scroll_elapsed"], 0)
