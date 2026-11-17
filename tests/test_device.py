from io import BytesIO
import unittest
from PIL import Image

from device import Device, render, rgb565, row_scroll_frame, title_scroll_frame, toast_image, toast_lines


class PlaybackTitleTest(unittest.TestCase):
    def test_long_notice_wraps_and_exit_progress_shares_the_toast(self):
        message = "网易云音乐命令执行失败，请检查登录状态和网络后重试。"
        rows = toast_lines(message)
        self.assertGreater(len(rows), 1)
        self.assertEqual("".join(rows), message)
        empty = toast_image("继续按住 Esc 退出", 0)
        filled = toast_image("继续按住 Esc 退出", 0.5)
        self.assertEqual(empty.size, filled.size)
        self.assertEqual(filled.getpixel((30, filled.height - 14)), (255, 255, 255, 255))
        self.assertEqual(empty.getpixel((30, empty.height - 14)), (255, 255, 255, 85))

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

    def test_selected_row_scroll_preserves_subtitle_and_loading(self):
        for subtitle in ("歌手", ""):
            with self.subTest(subtitle=subtitle):
                view = {"tab": 1, "heading": "歌单", "detail": "第 2 页", "selected": 7,
                        "rows": [("很长的歌曲或者歌单名称" * 6, subtitle)] * 10,
                        "loading_row": 7, "loading_frame": 3, "notice": "正在加载"}
                device = Device.__new__(Device)
                device.framebuffer = BytesIO()
                device.show(render(view | {"row_scroll": 0}))
                offset, interval = row_scroll_frame(view, 1.05)
                self.assertGreater(offset, 0)
                self.assertLessEqual(interval, 0.04)
                device.show_row_title(view, offset)
                self.assertEqual(device.framebuffer.getvalue(), rgb565(render(view | {"row_scroll": offset})))
