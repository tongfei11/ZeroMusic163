"""Cardputer Zero LCD, key input and 320×170 screen rendering."""

import os
import selectors
import struct
from contextlib import ExitStack
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

SIZE = (320, 170)
FRAMEBUFFER = "/dev/fb_lcd"
KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"
INPUT_EVENT = struct.Struct("@llHHi")
KEY_NAMES = {
    1: "esc", 14: "backspace", 15: "tab", 28: "enter", 57: "space",
    96: "enter", 103: "up", 105: "left", 106: "right", 108: "down",
}
for start, letters in ((2, "1234567890"), (16, "qwertyuiop"), (30, "asdfghjkl"), (44, "zxcvbnm")):
    KEY_NAMES.update({start + offset: char for offset, char in enumerate(letters)})


class Device:
    def __init__(self, framebuffer=FRAMEBUFFER, keyboard=KEYBOARD):
        with ExitStack() as resources:
            self.framebuffer = resources.enter_context(open(framebuffer, "wb", buffering=0))
            self.keyboard = resources.enter_context(open(keyboard, "rb", buffering=0))
            self.poll = resources.enter_context(selectors.DefaultSelector())
            self.poll.register(self.keyboard, selectors.EVENT_READ)
            self.resources = resources.pop_all()

    def show(self, image):
        if image.size != SIZE:
            raise ValueError("Expected a 320×170 image")
        pixels = bytearray(SIZE[0] * SIZE[1] * 2)
        for index, (red, green, blue) in enumerate(image.convert("RGB").getdata()):
            color = ((red & 248) << 8) | ((green & 252) << 3) | (blue >> 3)
            struct.pack_into("<H", pixels, index * 2, color)
        self.framebuffer.seek(0)
        pending = memoryview(pixels)
        while pending:
            written = self.framebuffer.write(pending)
            if not written:
                raise OSError("LCD write made no progress")
            pending = pending[written:]

    def events(self, timeout):
        if not self.poll.select(timeout):
            return []
        packets = self.keyboard.read(INPUT_EVENT.size * 32)
        if not packets:
            raise OSError("Keyboard disconnected")
        return [(KEY_NAMES[code], value)
                for _, _, kind, code, value in INPUT_EVENT.iter_unpack(packets)
                if kind == 1 and code in KEY_NAMES and value in (0, 1, 2)]

    def close(self):
        self.resources.close()


BACKGROUND = "#10151e"
SURFACE = "#243342"
TEXT = "#f5f7fb"
MUTED = "#acbbcb"
ACCENT = "#82e2cb"
TABS = ("搜索", "歌单", "播放", "队列")


@lru_cache(maxsize=32)
def font(size):
    paths = (
        os.environ.get("ZEROMUSIC_FONT"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "DejaVuSans.ttf",
    )
    for path in paths:
        if path:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def line(draw, value, x, y, width, size, color=TEXT):
    value = str(value or "")
    face = font(size)
    if draw.textlength(value, font=face) > width:
        clipped = value
        while clipped and draw.textlength(clipped + "…", font=face) > width:
            clipped = clipped[:-1]
        value = clipped + "…"
    draw.text((x, y), value, font=face, fill=color, anchor="lt")


def details(draw, value, x, y, width, bottom, size=13):
    face = font(size)
    for paragraph in str(value or "").splitlines():
        piece = ""
        for char in paragraph:
            if piece and draw.textlength(piece + char, font=face) > width:
                if y + size > bottom:
                    return
                line(draw, piece, x, y, width, size, MUTED)
                y += size + 5
                piece = ""
            piece += char
        if y + size > bottom:
            return
        line(draw, piece, x, y, width, size, MUTED)
        y += size + 5


@lru_cache(maxsize=2)
def qr_image(value):
    import qrcode

    qr = qrcode.QRCode(border=4)
    qr.add_data(value)
    qr.make(fit=True)
    qr.box_size = max(1, 141 // (qr.modules_count + 2 * qr.border))
    return qr.make_image().convert("RGB")


def timestamp(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{seconds:02d}"


def render(view):
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    tab = view.get("tab")
    y = 12
    if tab is not None:
        for index, label in enumerate(TABS):
            left = index * 80
            if index == tab:
                draw.rectangle((left + 3, 4, left + 76, 27), fill=SURFACE)
                draw.rectangle((left + 3, 26, left + 76, 27), fill=ACCENT)
            draw.text((left + 40, 16), label, font=font(14),
                      fill=ACCENT if index == tab else MUTED, anchor="mm")
        y = 36

    detail = view.get("detail")
    rows = view.get("rows") or []
    footer_top = 151
    body_bottom = 130 if view.get("notice") else footer_top - 2
    progress = view.get("progress")
    if view.get("qr"):
        code = qr_image(view["qr"])
        image.paste(code, (10, (footer_top - code.height) // 2))
        x = code.width + 21
        line(draw, view.get("heading"), x, 20, 311 - x, 17)
        details(draw, detail, x, 49, 311 - x, body_bottom)
    else:
        line(draw, view.get("heading"), 10, y, 300, 17)
        if rows:
            y += 24
            if detail:
                line(draw, detail, 10, y, 300, 12, MUTED)
                y += 16
            row_height = 47 if tab is None else 23
            rows_bottom = body_bottom - (23 if progress else 0)
            count = max(1, (rows_bottom - y + 1) // row_height)
            selected = view.get("selected")
            start = min(max(0, (selected or 0) - count + 1), max(0, len(rows) - count))
            for index, (title, subtitle) in enumerate(rows[start:start + count], start):
                active = index == selected
                if active:
                    draw.rectangle((5, y, 314, y + row_height - 1), fill=SURFACE)
                    draw.rectangle((5, y + 2, 6, y + row_height - 3), fill=ACCENT)
                stacked = tab is None or progress
                width = 297 if stacked or not subtitle else 176
                line(draw, title, 11, y + 4, width, 14)
                if subtitle and not progress:
                    if stacked:
                        line(draw, subtitle, 11, y + 30, 297, 12, MUTED)
                    else:
                        line(draw, subtitle, 195, y + 5, 112, 12, MUTED)
                y += row_height
        else:
            details(draw, detail, 10, y + 26, 300, body_bottom - (22 if progress else 0))

        if progress:
            position, duration = progress
            ratio = min(1, max(0, position / duration)) if duration else 0
            draw.rectangle((10, body_bottom - 18, 309, body_bottom - 15), fill=SURFACE)
            if ratio:
                draw.rectangle((10, body_bottom - 18, 10 + int(299 * ratio), body_bottom - 15), fill=ACCENT)
            total = timestamp(duration) if duration else "--:--"
            line(draw, f"{timestamp(position)} / {total}", 10, body_bottom - 11, 300, 10, MUTED)

    if view.get("notice"):
        if view.get("qr"):
            line(draw, view["notice"], x, footer_top - 19, 311 - x, 13, ACCENT)
        else:
            draw.rectangle((0, footer_top - 21, 319, footer_top - 1), fill=SURFACE)
            line(draw, view["notice"], 10, footer_top - 18, 300, 14, ACCENT)
    draw.line((0, footer_top, 319, footer_top), fill=SURFACE)
    line(draw, view.get("footer"), 10, footer_top + 3, 300, 12, MUTED)
    return image
