"""Cardputer Zero LCD, key input and 320×170 screen rendering."""

import os
import selectors
import struct
from contextlib import ExitStack
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps

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


BACKGROUND = "#fffdfd"
SURFACE = "#f9e5e7"
TEXT = "#291f23"
MUTED = "#6c5c63"
ACCENT = "#c92237"
WHITE = "#ffffff"
FOOTER_TOP = 151
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


def circle_image(image, source, x, y, size):
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    fitted = ImageOps.fit(source.convert("RGB"), (size, size), Image.Resampling.LANCZOS)
    image.paste(fitted, (x, y), mask)


def draw_tabs(draw, tab):
    draw.rectangle((0, 0, 319, 25), fill=ACCENT)
    for index, label in enumerate(TABS):
        left = index * 80
        if index == tab:
            draw.rectangle((left + 29, 23, left + 51, 24), fill=WHITE)
        draw.text((left + 40, 13), label, font=font(14), fill=WHITE, anchor="mm")


def draw_loading(draw, x, y, frame):
    box = (x - 5, y - 5, x + 5, y + 5)
    draw.ellipse(box, outline="#efc1c7", width=1)
    draw.arc(box, start=frame * 45, end=frame * 45 + 130, fill=ACCENT, width=2)


def draw_profile(image, draw, view):
    draw.rectangle((0, 26, 319, 66), fill="#b91d32")
    avatar = view.get("avatar")
    draw.ellipse((9, 30, 41, 62), fill=WHITE)
    if avatar is not None:
        circle_image(image, avatar, 10, 31, 31)
    else:
        draw.ellipse((20, 37, 30, 47), fill=ACCENT)
        draw.rounded_rectangle((16, 48, 34, 57), radius=4, fill=ACCENT)
    line(draw, view["profile_name"], 49, 39, 261, 16, WHITE)
    for index, (title, subtitle) in enumerate((view.get("rows") or [])[:4]):
        y = 67 + 21 * index
        active = index == view.get("selected")
        if active:
            draw.rounded_rectangle((5, y + 1, 314, y + 20), radius=4, fill=SURFACE)
            draw.rectangle((7, y + 4, 8, y + 17), fill=ACCENT)
        line(draw, title, 14, y + 4, 196 if subtitle else 280, 13, ACCENT if active else TEXT)
        if subtitle:
            line(draw, subtitle, 213, y + 5, 83, 11, MUTED)
        if index == view.get("loading_row"):
            draw_loading(draw, 306, y + 10, view["loading_frame"])
        else:
            draw.line((305, y + 7, 308, y + 10, 305, y + 13), fill=ACCENT if active else MUTED, width=1)


def draw_progress(draw, progress, x, y, width):
    position, duration = progress or (0, 0)
    ratio = min(1, max(0, position / duration)) if duration else 0
    marker = x + round((width - 1) * ratio)
    draw.rounded_rectangle((x, y, x + width - 1, y + 2), radius=1, fill=SURFACE)
    if ratio:
        draw.rounded_rectangle((x, y, marker, y + 2), radius=1, fill=ACCENT)
        draw.ellipse((marker - 2, y - 1, marker + 2, y + 3), fill=ACCENT)
    total = timestamp(duration) if duration else "--:--"
    line(draw, f"{timestamp(position)} / {total}", x, y + 7, width, 10, MUTED)


def draw_record(image, draw, artwork):
    cx, cy = 251, 92
    draw.ellipse((193, 34, 309, 150), fill="#f2e8ea")
    draw.ellipse((197, 38, 305, 146), fill="#19181b", outline="#100f10", width=2)
    for radius in range(31, 53, 3):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline="#333035")
    draw.ellipse((222, 63, 280, 121), fill="#a11b2d")
    if artwork is not None:
        circle_image(image, artwork, 223, 64, 57)
    else:
        draw.ellipse((226, 67, 276, 117), fill="#e13248", outline="#ff6878")
        draw.ellipse((238, 79, 264, 105), outline="#f56e7c")
        draw.ellipse((248, 89, 254, 95), fill="#fff1f2")

    draw.ellipse((290, 29, 305, 44), fill="#777079")
    draw.ellipse((293, 32, 302, 41), fill="#f0e6e9")
    points = [(297, 37), (293, 51), (286, 58), (279, 63)]
    draw.line([(x + 1, y + 1) for x, y in points], fill="#40363f", width=5, joint="curve")
    draw.line(points, fill="#ece1e5", width=3, joint="curve")
    draw.line((282, 61, 274, 67), fill="#777079", width=6)
    draw.line((281, 61, 274, 66), fill=WHITE, width=3)


def draw_playback(image, draw, view):
    playback = view["playback"]
    status = playback.get("status")
    playing = status == "playing"
    states = {"idle": "待播放", "playing": "播放中", "paused": "已暂停", "starting": "载入中",
              "ended": "已播完", "failed": "播放失败"}
    state = states.get(status, "")
    mode = playback.get("mode") or ""
    stats = " · ".join(part for part in (state, mode, f"音量 {playback.get('volume', 0)}") if part)
    line(draw, view.get("heading"), 10, 34, 180, 17)
    line(draw, view.get("detail"), 10, 56, 180, 12, MUTED)
    draw_progress(draw, view.get("progress"), 10, 78, 176)
    line(draw, stats, 10, 101, 177, 11, ACCENT if status == "failed" else MUTED)
    disabled = status in ("idle", "starting")
    draw.ellipse((73, 111, 101, 139), fill="#a19396" if disabled else ACCENT)
    if playing:
        draw.rounded_rectangle((82, 118, 85, 132), radius=1, fill=WHITE)
        draw.rounded_rectangle((89, 118, 92, 132), radius=1, fill=WHITE)
    elif status == "starting":
        for x in (82, 87, 92):
            draw.ellipse((x - 1, 124, x + 1, 126), fill=WHITE)
    else:
        draw.polygon(((84, 117), (84, 133), (95, 125)), fill=WHITE)
    line(draw, "Z 上一首", 10, 121, 60, 11, MUTED if disabled else TEXT)
    line(draw, "C 下一首", 125, 121, 62, 11, MUTED if disabled else TEXT)
    actions = {"idle": "等待选歌", "playing": "空格 暂停", "starting": "正在载入", "failed": "Enter 重试"}
    line(draw, actions.get(status, "空格 播放"), 65, 140, 65, 10, MUTED if disabled else ACCENT)

    draw_record(image, draw, playback.get("artwork"))
    if playback.get("liked"):
        draw.ellipse((300, 133, 309, 142), fill=ACCENT)
        draw.ellipse((307, 133, 316, 142), fill=ACCENT)
        draw.polygon(((301, 140), (315, 140), (308, 148)), fill=ACCENT)


def render(view):
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    tab = view.get("tab")
    y = 12
    if tab is not None:
        draw_tabs(draw, tab)
        y = 34

    if view.get("playback") or view.get("profile_name"):
        playback = view.get("playback")
        if playback:
            draw_playback(image, draw, view)
            footer = "F/X 音量 · S 模式 · L 歌词 · H 收藏 · V 场景"
        else:
            draw_profile(image, draw, view)
            footer = view.get("footer")
        draw_footer(draw, footer, view.get("notice"))
        return image

    detail = view.get("detail")
    rows = view.get("rows") or []
    footer_top = FOOTER_TOP
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
                loading = index == view.get("loading_row")
                width = (282 if loading else 297) if stacked or not subtitle else 176
                line(draw, title, 11, y + 4, width, 14)
                if subtitle and not progress:
                    if stacked:
                        line(draw, subtitle, 11, y + 30, 297, 12, MUTED)
                    else:
                        line(draw, subtitle, 195, y + 5, 100 if loading else 112, 12, MUTED)
                if loading:
                    draw_loading(draw, 306, y + 11, view["loading_frame"])
                y += row_height
        else:
            details(draw, detail, 10, y + 26, 300, body_bottom - (22 if progress else 0))

        if progress:
            draw_progress(draw, progress, 10, body_bottom - 18, 300)

    if view.get("notice"):
        if view.get("qr"):
            line(draw, view["notice"], x, footer_top - 19, 311 - x, 13, ACCENT)
        else:
            draw.rectangle((0, footer_top - 21, 319, footer_top - 1), fill=SURFACE)
            line(draw, view["notice"], 10, footer_top - 18, 300, 14, ACCENT)
    draw_footer(draw, view.get("footer"))
    return image


def draw_footer(draw, footer, notice=None):
    if notice:
        draw.rectangle((0, FOOTER_TOP, 319, 169), fill=SURFACE)
    else:
        draw.line((0, FOOTER_TOP, 319, FOOTER_TOP), fill=SURFACE)
    line(draw, notice or footer, 10, FOOTER_TOP + 3, 300, 12, ACCENT if notice else MUTED)
