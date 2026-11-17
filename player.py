"""Queue and session state owned by the device UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from random import randrange
import re


MODES = ("顺序", "随机", "单曲循环")
STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def artists(track):
    names = track.get("fullArtists") or track.get("artists") or []
    if isinstance(names, str):
        return names
    return " / ".join(str(item.get("name", "") if isinstance(item, dict) else item)
                      for item in names)


def playable(track):
    flag = track.get("playFlag")
    return bool(track.get("id") and track.get("originalId") and track.get("visible") is not False and
                flag not in (False, "false", "0"))


def track_rows(tracks):
    return [(track.get("name") or "未知歌曲", artists(track) if playable(track)
             else "暂不可播 · " + artists(track)) for track in tracks]


def lyric_lines(text):
    timed, plain = [], []
    for line in text.splitlines():
        stamps = STAMP.findall(line)
        words = STAMP.sub("", line).strip()
        if not words:
            continue
        if stamps:
            timed.extend((int(minutes) * 60 + float(seconds), words) for minutes, seconds in stamps)
        elif not words.startswith("["):
            plain.append((0, words))
    return sorted(timed) if timed else plain


@dataclass
class Player:
    current: dict | None = None
    upcoming: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    position: float = 0
    duration: float = 0
    status: str = "idle"
    volume: int = 75
    mode: int = 0
    seen_playing: bool = False

    def jump(self, track, remaining, *, remember=True):
        if remember and self.current is not None and self.current.get("id") != track.get("id"):
            self.history.append(self.current)
        self.current, self.upcoming = track, remaining
        self.position = self.duration = 0
        self.status = "starting"
        self.seen_playing = False

    def apply_status(self, state):
        status = state.get("status")
        volume = state.get("volume")
        if isinstance(volume, (int, float)):
            self.volume = int(volume)
        if status in ("playing", "paused"):
            position, duration = state.get("position"), state.get("duration")
            if isinstance(position, (int, float)):
                self.position = number(position)
                self.seen_playing = True
            if isinstance(duration, (int, float)) and duration > 0:
                self.duration = number(duration)
            self.status = status
            return False
        if status == "unknown" or self.status == "paused":
            return False
        if status == "stopped" and self.seen_playing and self.status == "playing":
            # The CLI resets the position at EOF. Keep the most recent playing position.
            self.status = "ended"
            if self.duration and self.position >= self.duration - 3:
                self.position = self.duration
                return True
            self.status = "failed"
        return False

    def next_index(self):
        if not self.upcoming:
            return None
        return randrange(len(self.upcoming)) if self.mode == 1 else 0


def number(value):
    return max(0.0, float(value)) if isinstance(value, (int, float)) else 0.0
