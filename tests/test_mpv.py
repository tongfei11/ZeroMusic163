import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from mpv_ipc import Mpv, MpvError
from playback_resume import load_resume, save_resume
from player import Player


class MpvTest(unittest.TestCase):
    def setUp(self):
        self.mpv = Mpv("/tmp/test-mpv.sock", lambda: False)

    def load_events(self, *events):
        channel = Mock()
        channel.__enter__ = lambda self: self
        channel.__exit__ = lambda *_: None
        data = b"".join((json.dumps(event) + "\n").encode() for event in events)
        channel.recv.side_effect = [data[:11], data[11:], b""]
        with patch("mpv_ipc.socket.socket", return_value=channel):
            return self.mpv.load_file("http://example.com/song")

    def test_load_waits_for_the_new_entry_event(self):
        self.assertTrue(self.load_events(
            {"event": "end-file", "playlist_entry_id": 1, "reason": "stop"},
            {"request_id": 1, "error": "success"},
            {"event": "start-file", "playlist_entry_id": 2},
            {"event": "file-loaded"},
        ))

    def test_load_does_not_accept_only_the_command_reply(self):
        self.assertFalse(self.load_events({"request_id": 1, "error": "success"}))

    def test_new_file_error_is_reported(self):
        with self.assertRaises(MpvError):
            self.load_events(
                {"request_id": 1, "error": "success"},
                {"event": "start-file", "playlist_entry_id": 2},
                {"event": "end-file", "playlist_entry_id": 2, "reason": "error"},
            )

    def test_transport_failure_is_retryable_and_bad_format_is_unavailable(self):
        for failure, retryable in (("network error", True), ("unrecognized file format", False)):
            with self.subTest(failure=failure), self.assertRaises(MpvError) as caught:
                self.load_events(
                    {"request_id": 1, "error": "success"},
                    {"event": "start-file", "playlist_entry_id": 2},
                    {"event": "end-file", "playlist_entry_id": 2, "reason": "error", "file_error": failure},
                )
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(caught.exception.unavailable, not retryable)

    def test_resume_seeks_before_unpausing(self):
        self.mpv.start = Mock()
        self.mpv.properties = Mock(return_value={"playlist/0/id": 1})
        self.mpv.load_file = Mock(return_value=True)
        self.mpv.command = Mock()
        self.mpv.play("http://example.com/song", 60, 41.4)
        self.mpv.load_file.assert_called_once_with("http://example.com/song", paused=True)
        self.assertEqual(self.mpv.command.call_args_list, [
            call("set_property", "volume", 60),
            call("set_property", "pause", True),
            call("seek", 41.4, "absolute+exact"),
            call("set_property", "pause", False),
        ])

    def test_playback_session_is_replaced_atomically(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "playback.json"
            self.assertEqual(load_resume(path), {})
            first = {"track": {"name": "宁夏"}, "position": 41.4}
            save_resume(path, first)
            self.assertEqual(load_resume(path), first)
            with self.assertRaises(ValueError):
                save_resume(path, {"position": float("nan")})
            self.assertEqual(load_resume(path), first)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_missing_ipc_data_does_not_reset_playing_progress(self):
        values = {"idle-active": None, "pause": None, "time-pos": None,
                  "duration": None, "volume": None}
        self.mpv.properties = lambda *_: dict(values)
        player = Player()
        player.apply_status({"status": "playing", "position": 43, "duration": 175})
        self.assertEqual(self.mpv.state()["status"], "unknown")
        self.assertFalse(player.apply_status(self.mpv.state()))
        self.assertEqual((player.status, player.position, player.duration), ("playing", 43, 175))

        values.update({"idle-active": False, "pause": False, "time-pos": 45})
        self.assertFalse(player.apply_status(self.mpv.state()))
        self.assertEqual((player.status, player.position, player.duration), ("playing", 45, 175))
