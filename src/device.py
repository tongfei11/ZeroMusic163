"""Cardputer Zero LCD, key input and 320×170 screen rendering."""

import os
import selectors
import struct
from contextlib import ExitStack
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

SIZE = (320, 170)
FRAMEBUFFER = "/dev/fb_lcd"
KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"
INPUT_EVENT = struct.Struct("@llHHi")
KEY_NAMES = {
    1: "esc", 14: "backspace", 15: "tab", 28: "enter", 57: "space",
    96: "enter", 103: "up", 105: "left", 106: "right", 108: "down",
    114: "volume-down", 115: "volume-up", 138: "help",
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
        self.pixels = rgb565(image)
        self.write_frame()

    def show_title(self, title, offset):
        stride, strip = playback_title_pixels(title)
        width = PLAYBACK_TITLE_WIDTH * 2
        for row in range(PLAYBACK_TITLE_HEIGHT):
            start = ((PLAYBACK_TITLE_Y + row) * SIZE[0] + PLAYBACK_TITLE_X) * 2
            source = row * stride + offset * 2
            self.pixels[start:start + width] = strip[source:source + width]
        self.write_frame()

    def write_frame(self):
        self.framebuffer.seek(0)
        pending = memoryview(self.pixels)
        while pending:
            written = self.framebuffer.write(pending)
            if not written:
                raise OSError("LCD write made no progress")
            pending = pending[written:]

    def show_row_title(self, view, offset):
        title, x, y, width = selected_row_title(view)
        stride, strip = row_title_pixels(title, width)
        for row in range(18):
            start = ((y + row) * SIZE[0] + x) * 2
            source = row * stride + offset * 2
            self.pixels[start:start + width * 2] = strip[source:source + width * 2]
        self.write_frame()

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


def rgb565(image):
    if image.mode != "RGB":
        image = image.convert("RGB")
    red, green, blue = image.split()
    low = ImageChops.add(green.point(GREEN_LOW), blue.point(BLUE_LOW))
    high = ImageChops.add(red.point(RED_HIGH), green.point(GREEN_HIGH))
    return bytearray(Image.merge("LA", (low, high)).tobytes())


RED_HIGH = [value & 248 for value in range(256)]
GREEN_HIGH = [value >> 5 for value in range(256)]
GREEN_LOW = [(value & 28) << 3 for value in range(256)]
BLUE_LOW = [value >> 3 for value in range(256)]


BACKGROUND = "#fffdfd"
SURFACE = "#f9e5e7"
TEXT = "#291f23"
MUTED = "#6c5c63"
ACCENT = "#c92237"
WHITE = "#ffffff"
FOOTER_TOP = 151
TABS = ("搜索", "歌单", "播放", "队列")
PLAYBACK_TITLE_WIDTH = 193
PLAYBACK_TITLE_HEIGHT = 20
PLAYBACK_TITLE_X, PLAYBACK_TITLE_Y = 10, 35
PLAYBACK_TITLE_SIZE = 17
TITLE_SCROLL_SPEED = 46
TITLE_SCROLL_PAUSE = 0.55
TITLE_SCROLL_FRAME = 1 / 25


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


@lru_cache(maxsize=32)
def title_overflow(title):
    _, _, right, _ = font(PLAYBACK_TITLE_SIZE).getbbox(title, anchor="lt")
    return max(0, right - PLAYBACK_TITLE_WIDTH)


def title_scroll_frame(title, elapsed):
    overflow = title_overflow(title)
    return scroll_frame(overflow, elapsed)


def scroll_frame(overflow, elapsed):
    if not overflow:
        return 0, 0.12
    travel = overflow / TITLE_SCROLL_SPEED
    phase = max(0.0, elapsed) % (travel + 2 * TITLE_SCROLL_PAUSE)
    if phase < TITLE_SCROLL_PAUSE:
        return 0, min(0.12, TITLE_SCROLL_PAUSE - phase)
    if phase >= TITLE_SCROLL_PAUSE + travel:
        return overflow, min(0.12, travel + 2 * TITLE_SCROLL_PAUSE - phase)
    return int((phase - TITLE_SCROLL_PAUSE) * TITLE_SCROLL_SPEED), TITLE_SCROLL_FRAME


def selected_row_title(view):
    rows = view.get("rows") or []
    selected = view.get("selected")
    if view.get("profile_name") or view.get("qr") or view.get("playback") or selected is None:
        return None
    if not 0 <= selected < len(rows):
        return None
    top = 58 + (16 if view.get("detail") else 0) if view.get("tab") is not None else 36
    height = 23 if view.get("tab") is not None else 47
    bottom = FOOTER_TOP - 2
    if view.get("progress"):
        bottom -= 23
    count = max(1, (bottom - top + 1) // height)
    start = min(max(0, selected - count + 1), max(0, len(rows) - count))
    title, subtitle = rows[selected]
    loading = selected == view.get("loading_row")
    stacked = view.get("tab") is None or view.get("progress")
    width = (282 if loading else 297) if stacked or not subtitle else 176
    return str(title), 11, top + (selected - start) * height + 4, width


@lru_cache(maxsize=8)
def row_title_image(title, width):
    right = font(14).getbbox(title, anchor="lt")[2]
    image = Image.new("RGB", (max(width, right), 18), SURFACE)
    ImageDraw.Draw(image).text((0, 0), title, font=font(14), fill=TEXT, anchor="lt")
    return image


@lru_cache(maxsize=1)
def row_title_pixels(title, width):
    image = row_title_image(title, width)
    return image.width * 2, rgb565(image)


def row_scroll_frame(view, elapsed):
    row = selected_row_title(view)
    if row is None:
        return 0, 0.12
    title, _, _, width = row
    return scroll_frame(row_title_image(title, width).width - width, elapsed)


@lru_cache(maxsize=4)
def playback_title_image(title):
    strip = Image.new("RGB", (PLAYBACK_TITLE_WIDTH + title_overflow(title), PLAYBACK_TITLE_HEIGHT), BACKGROUND)
    ImageDraw.Draw(strip).text((0, 0), title, font=font(PLAYBACK_TITLE_SIZE), fill=TEXT, anchor="lt")
    return strip


def playback_title_strip(title, offset):
    return playback_title_image(title).crop((offset, 0, offset + PLAYBACK_TITLE_WIDTH, PLAYBACK_TITLE_HEIGHT))


@lru_cache(maxsize=1)
def playback_title_pixels(title):
    image = playback_title_image(title)
    return image.width * 2, rgb565(image)


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
    cx, cy = 260, 81
    draw.ellipse((214, 35, 306, 127), fill="#f2e8ea")
    draw.ellipse((217, 38, 303, 124), fill="#19181b", outline="#100f10", width=2)
    for radius in range(25, 42, 3):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                     outline="#333035")
    draw.ellipse((237, 58, 283, 104), fill="#a11b2d")
    if artwork is not None:
        circle_image(image, artwork, 238, 59, 45)
    else:
        draw.ellipse((240, 61, 280, 101), fill="#e13248", outline="#ff6878")
        draw.ellipse((249, 70, 271, 92), outline="#f56e7c")
        draw.ellipse((258, 79, 262, 83), fill="#fff1f2")

    draw.ellipse((292, 32, 304, 44), fill="#777079")
    draw.ellipse((295, 35, 301, 41), fill="#f0e6e9")
    arm = [(298, 38), (295, 49), (283, 60)]
    draw.line([(x + 1, y + 1) for x, y in arm], fill="#40363f", width=5, joint="curve")
    draw.line(arm, fill="#ece1e5", width=3, joint="curve")
    draw.line((285, 58, 279, 64), fill="#777079", width=5)
    draw.line((284, 58, 279, 63), fill=WHITE, width=2)


def draw_volume(draw, volume, x, y):
    draw.polygon(((x, y - 3), (x + 4, y - 3), (x + 9, y - 7),
                  (x + 9, y + 7), (x + 4, y + 3), (x, y + 3)), fill=MUTED)
    if volume <= 0:
        draw.line((x + 12, y - 3, x + 17, y + 2), fill=MUTED, width=1)
        draw.line((x + 12, y + 2, x + 17, y - 3), fill=MUTED, width=1)
        return
    for radius in (5, 8, 11)[:(min(volume, 100) - 1) // 33 + 1]:
        draw.arc((x + 8 - radius, y - radius, x + 8 + radius, y + radius),
                 start=-48, end=48, fill=MUTED, width=1)


def draw_mode(draw, center, y, mode):
    x = center - 9
    if mode == "随机":
        draw.line((x, y - 5, x + 4, y - 5, x + 14, y + 5, x + 18, y + 5), fill=TEXT, width=2)
        draw.line((x, y + 5, x + 4, y + 5, x + 14, y - 5, x + 18, y - 5), fill=TEXT, width=2)
    elif mode == "单曲循环":
        draw.line((x, y - 2, x, y - 6, x + 18, y - 6), fill=TEXT, width=2)
        draw.line((x, y + 6, x + 18, y + 6, x + 18, y + 2), fill=TEXT, width=2)
        draw.line((center - 2, y - 1, center, y - 3, center, y + 3), fill=TEXT, width=1)
        draw.polygon(((x + 3, y + 2), (x - 2, y + 6), (x + 3, y + 10)), fill=TEXT)
        draw.polygon(((x + 15, y - 10), (x + 20, y - 6), (x + 15, y - 2)), fill=TEXT)
        return
    else:
        for dy in (-5, 5):
            draw.line((x, y + dy, x + 18, y + dy), fill=TEXT, width=2)
    for dy in (-5, 5):
        draw.polygon(((x + 15, y + dy - 3), (x + 20, y + dy), (x + 15, y + dy + 3)), fill=TEXT)


def draw_skip(draw, x, y, direction, color):
    points = ((x - 4 * direction, y - 7), (x + 4 * direction, y), (x - 4 * direction, y + 7))
    draw.polygon(points, fill=color)
    bar = x + 6 * direction
    draw.line((bar, y - 7, bar, y + 7), fill=color, width=2)


def draw_favorite(draw, x, y, liked):
    # The two lobes and lower point share their edges in the filled state.
    if liked:
        draw.ellipse((x - 9, y - 7, x, y + 3), fill=ACCENT)
        draw.ellipse((x, y - 7, x + 9, y + 3), fill=ACCENT)
        draw.polygon(((x - 8, y), (x + 8, y), (x, y + 8)), fill=ACCENT)
    else:
        draw.arc((x - 9, y - 7, x, y + 3), start=155, end=360, fill=ACCENT, width=2)
        draw.arc((x, y - 7, x + 9, y + 3), start=180, end=25, fill=ACCENT, width=2)
        draw.line((x - 9, y, x, y + 8, x + 9, y), fill=ACCENT, width=2, joint="curve")


def draw_transport(draw, playback):
    mode, previous, toggle, following, favorite = (52, 109, 165, 220, 274)
    y = 153
    status = playback.get("status")
    loading = status in ("starting", "retrying")
    disabled = status in ("idle", "starting")
    draw.rectangle((0, 136, 319, 169), fill=WHITE)
    draw.line((0, 136, 319, 136), fill=SURFACE)
    draw_mode(draw, mode, y, playback.get("mode"))
    draw_skip(draw, previous, y, -1, MUTED if disabled else TEXT)
    draw_skip(draw, following, y, 1, MUTED if disabled else TEXT)
    button_color = SURFACE if loading else "#9b8b91" if status == "idle" else ACCENT
    draw.ellipse((toggle - 13, y - 13, toggle + 13, y + 13), fill=button_color)
    if status in ("playing", "buffering"):
        draw.rounded_rectangle((toggle - 5, y - 6, toggle - 2, y + 6), radius=1, fill=WHITE)
        draw.rounded_rectangle((toggle + 2, y - 6, toggle + 5, y + 6), radius=1, fill=WHITE)
    elif loading:
        draw_loading(draw, toggle, y, playback.get("loading_frame", 0))
    else:
        draw.polygon(((toggle - 3, y - 6), (toggle - 3, y + 6), (toggle + 6, y)), fill=WHITE)
    like_frame = playback.get("like_frame")
    if like_frame is not None:
        draw_loading(draw, favorite, y, like_frame)
    else:
        draw_favorite(draw, favorite, y, playback.get("liked"))


def draw_playback(image, draw, view):
    playback = view["playback"]
    title = str(view.get("heading") or "")
    image.paste(playback_title_strip(title, view.get("title_scroll", 0)), (PLAYBACK_TITLE_X, PLAYBACK_TITLE_Y))
    line(draw, view.get("detail"), 10, 56, 163, 12, MUTED)
    draw_volume(draw, playback.get("volume", 0), 183, 63)
    draw_progress(draw, view.get("progress"), 10, 74, 191)
    for index, lyric in enumerate((view.get("lyric_preview") or [])[:2]):
        line(draw, lyric, 10, 93 + index * 13, 193, 11, ACCENT if index == 0 else MUTED)
    draw_record(image, draw, playback.get("artwork"))
    if playback.get("status") == "failed":
        line(draw, "播放失败", 10, 121, 190, 11, ACCENT)
    draw_transport(draw, playback)


def render(view):
    image = render_screen(view)
    progress = view.get("exit_progress")
    message = "继续按住 Esc 退出" if progress is not None else view.get("notice")
    if message:
        toast = toast_image(message, progress)
        image.paste(toast, ((SIZE[0] - toast.width) // 2, (SIZE[1] - toast.height) // 2), toast)
    return image


@lru_cache(maxsize=16)
def toast_lines(message):
    rows = []
    face = font(15)
    for paragraph in message.splitlines():
        row = ""
        for char in paragraph:
            if row and face.getlength(row + char) > 252:
                rows.append(row.rstrip())
                row = "" if char.isspace() else char
            else:
                row += char
        rows.append(row)
    return rows


def toast_image(message, progress):
    rows = toast_lines(message)
    face = font(15)
    width = min(284, max(150, round(max(map(face.getlength, rows), default=0)) + 32))
    height = 22 + 20 * min(6, len(rows)) + (12 if progress is not None else 0)
    toast = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(toast)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=9, fill=(0, 0, 0, 200))
    for index, row in enumerate(rows[:6]):
        if index == 5 and len(rows) > 6:
            line(draw, row + "…", 16, 12 + index * 20, width - 32, 15, WHITE)
        else:
            draw.text((width // 2, 12 + index * 20), row, fill=WHITE, font=face, anchor="mt")
    if progress is not None:
        left, right = 16, width - 17
        top = height - 16
        draw.rounded_rectangle((left, top, right, top + 4), radius=2, fill="#555555")
        filled = round((right - left + 1) * progress)
        if filled:
            draw.rounded_rectangle((left, top, left + filled - 1, top + 4), radius=2, fill=WHITE)
    return toast


def render_screen(view):
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    tab = view.get("tab")
    y = 12
    if tab is not None:
        draw_tabs(draw, tab)
        y = 34

    if view.get("playback"):
        draw_playback(image, draw, view)
        return image
    if view.get("profile_name"):
        draw_profile(image, draw, view)
        draw_footer(draw, view.get("footer"))
        return image

    detail = view.get("detail")
    rows = view.get("rows") or []
    body_bottom = FOOTER_TOP - 2
    progress = view.get("progress")
    if view.get("qr"):
        code = qr_image(view["qr"])
        image.paste(code, (10, (FOOTER_TOP - code.height) // 2))
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

        if "row_scroll" in view:
            row = selected_row_title(view)
            if row is not None:
                title, rx, ry, width = row
                strip = row_title_image(title, width)
                if strip.width > width:
                    offset = view["row_scroll"]
                    image.paste(strip.crop((offset, 0, offset + width, 18)), (rx, ry))

        if progress:
            draw_progress(draw, progress, 10, body_bottom - 18, 300)

    draw_footer(draw, view.get("footer"))
    return image


def draw_footer(draw, footer):
    draw.line((0, FOOTER_TOP, 319, FOOTER_TOP), fill=SURFACE)
    line(draw, footer, 10, FOOTER_TOP + 3, 300, 12, MUTED)
