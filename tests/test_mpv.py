import json
import unittest
from unittest.mock import Mock, patch

from mpv_ipc import Mpv, MpvError
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
