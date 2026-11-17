from io import BytesIO
import unittest
from PIL import Image

from device import Device, render, rgb565, title_scroll_frame


class PlaybackTitleTest(unittest.TestCase):
    def test_framebuffer_color_encoding(self):
        image = Image.new("RGB", (4, 1))
        image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
        self.assertEqual(rgb565(image), b"\x00\xf8\xe0\x07\x1f\x00\xff\xff")

    def test_scrolling_title_updates_match_a_full_frame(self):
        title = "很长的歌曲名称" * 6
        view = {"tab": 2, "heading": title, "detail": "歌手", "progress": (2, 100),
                "playback": {"status": "playing", "volume": 75}}
        device = Device.__new__(Device)
        device.framebuffer = BytesIO()
        device.show(render(view))

        offset, interval = title_scroll_frame(title, 1.05)
        self.assertGreaterEqual(offset, 22)
        self.assertLessEqual(interval, 0.04)
        device.show_title(title, offset)
        self.assertEqual(device.framebuffer.getvalue(), rgb565(render(view | {"title_scroll": offset})))

    def test_short_title_keeps_the_normal_refresh_interval(self):
        self.assertEqual(title_scroll_frame("晴天", 5), (0, 0.12))
