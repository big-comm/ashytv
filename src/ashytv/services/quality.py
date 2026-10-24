"""Channel quality variants and automatic quality selection.

IPTV providers usually publish one entry per quality ("NEWS FHD", "NEWS HD",
"NEWS SD"). `split_quality` turns such a name into a base name plus a quality
rank so the variants can be shown as a single channel, and
`QualityController` decides when to switch between them — a simple adaptive
bitrate strategy: step down when playback keeps stalling, step back up after a
long stable period with a full buffer, and fall back when a variant is
offline. It has no GTK dependency so it can be unit tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import normalize

# Rank: higher is better. 0 = unknown (single-quality channel).
_QUALITY_TOKENS = [
    (re.compile(r"\b(?:4K|UHD|2160P)\b", re.I), 4, "4K"),
    (re.compile(r"\b(?:FHD|FULL\s*HD|1080[PI])\b", re.I), 3, "FHD"),
    (re.compile(r"\b(?:HD|720P)\b", re.I), 2, "HD"),
    (re.compile(r"\b(?:SD|480P|576P)\b", re.I), 1, "SD"),
]
# Tokens that describe the encoding or a backup feed, not the channel name.
_NOISE_RE = re.compile(r"\b(?:H\.?26[45]|HEVC|AVC)\b|[\[(]\s*ALT\s*[\])]|\bALT\b", re.I)
_TRIM = " -|:·•_"

LABELS = {4: "4K", 3: "FHD", 2: "HD", 1: "SD"}


def split_quality(name: str) -> tuple[str, int]:
    """'NEWS 24 FHD' -> ('NEWS 24', 3); 'Kids Channel' -> ('Kids Channel', 0)."""
    base = name
    rank = 0
    for pattern, value, _label in _QUALITY_TOKENS:
        if pattern.search(base):
            rank = max(rank, value)
            base = pattern.sub(" ", base)
    base = _NOISE_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip(_TRIM).strip()
    if not base:  # the whole name was a quality token: keep it as is
        return name.strip(), rank
    return base, rank


def base_key(base_name: str) -> str:
    """Grouping key for variants: accent- and case-insensitive."""
    return re.sub(r"\s+", " ", normalize(base_name)).strip()


def quality_label(rank: int) -> str:
    return LABELS.get(rank, "")


# ---- adaptive selection ---------------------------------------------------

STALL_WINDOW = 60          # stalls counted within this many seconds
STALLS_TO_STEP_DOWN = 2    # this many stalls in the window -> lower quality
LONG_STALL = 6             # a single stall this long -> lower quality
STABLE_TO_STEP_UP = 180    # seconds without stalls before trying higher
UPGRADE_GRACE = 60         # a stall this soon after stepping up = it failed
UPGRADE_BACKOFF = 900      # after a failed step up, wait this long


@dataclass
class QualityController:
    """Chooses among variants ordered best-first (index 0 = best)."""

    levels: int
    index: int = 0
    auto: bool = True
    _stalls: list[float] = field(default_factory=list)
    _stall_started: float | None = None
    _last_change: float = 0.0
    _raised_at: float | None = None
    _no_raise_until: float = 0.0

    def start(self, now: float) -> None:
        self._last_change = now
        self._stalls.clear()
        self._stall_started = None

    def select(self, index: int, now: float) -> None:
        """Manual choice: stop adapting."""
        self.auto = False
        self._move(index, now)

    def set_auto(self, now: float) -> None:
        self.auto = True
        self._last_change = now

    def stall_started(self, now: float) -> int | None:
        self._stall_started = now
        self._stalls = [t for t in self._stalls if now - t < STALL_WINDOW] + [now]
        if self._raised_at is not None and now - self._raised_at < UPGRADE_GRACE:
            # The higher quality did not hold: go back and wait before retrying.
            self._no_raise_until = now + UPGRADE_BACKOFF
            self._raised_at = None
            return self._step_down(now)
        if len(self._stalls) >= STALLS_TO_STEP_DOWN:
            return self._step_down(now)
        return None

    def stall_ended(self, _now: float) -> None:
        self._stall_started = None

    def tick(self, now: float, buffer_full: bool) -> int | None:
        """Call periodically; may return a new index."""
        if self._stall_started is not None:
            if now - self._stall_started >= LONG_STALL:
                return self._step_down(now)
            return None
        if self._raised_at is not None and now - self._raised_at >= UPGRADE_GRACE:
            self._raised_at = None  # the step up held
        recent = [t for t in self._stalls if now - t < STABLE_TO_STEP_UP]
        if (self.auto and self.index > 0 and buffer_full and not recent
                and now - self._last_change >= STABLE_TO_STEP_UP
                and now >= self._no_raise_until):
            self._move(self.index - 1, now)
            self._raised_at = now
            return self.index
        return None

    def failed(self, now: float) -> int | None:
        """The current variant does not play: fall back to the next one."""
        if self.index + 1 < self.levels:
            self._move(self.index + 1, now)
            return self.index
        return None

    def _step_down(self, now: float) -> int | None:
        if not self.auto or self.index + 1 >= self.levels:
            return None
        self._move(self.index + 1, now)
        return self.index

    def _move(self, index: int, now: float) -> None:
        self.index = max(0, min(self.levels - 1, index))
        self._last_change = now
        self._stalls.clear()
        self._stall_started = None
