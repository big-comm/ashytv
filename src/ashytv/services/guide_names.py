"""Linking channels to the programme guide by name.

Used when a channel has no tvg-id, or one the guide doesn't carry: its name
is compared with the guide's <display-name>s — conservatively, as "better no
guide than a wrong one": the normalized name must belong to exactly ONE guide
channel. GTK-free (unit tested).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .text import normalize as _plain

_BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Quality/format words, dropped as whole words.
_NOISE = frozenset({"fhd", "fullhd", "hd", "sd", "uhd", "4k", "8k", "h264", "h265",
                    "hevc", "hdr", "60fps", "alt", "backup"})


def normalize_name(name: str) -> str:
    """'Canal Ação FHD [Alt] (BR)' -> 'canal acao' (lowercase, no accents,
    no quality words, no [brackets] or (parentheses))."""
    text = _NON_ALNUM_RE.sub(" ", _BRACKETS_RE.sub(" ", _plain(name)))
    words = text.split()
    kept: list[str] = []
    i = 0
    while i < len(words):
        if words[i] == "full" and i + 1 < len(words) and words[i + 1] == "hd":
            i += 2  # "Full HD" as two words
            continue
        if words[i] not in _NOISE:
            kept.append(words[i])
        i += 1
    return " ".join(kept)


class GuideNameIndex:
    """Guide channels by normalized display-name; a name shared by two
    different ids maps to nothing."""

    def __init__(self) -> None:
        self.ids: set[str] = set()          # every guide channel id (lowercase)
        self._by_name: dict[str, str] = {}
        self._ambiguous: set[str] = set()

    def add(self, channel_id: str, names: Iterable[str]) -> None:
        cid = channel_id.lower()
        if not cid:
            return
        self.ids.add(cid)
        for key in {normalize_name(n) for n in names}:
            if not key or key in self._ambiguous:
                continue
            prev = self._by_name.get(key)
            if prev is None:
                self._by_name[key] = cid
            elif prev != cid:
                del self._by_name[key]
                self._ambiguous.add(key)

    def match(self, name: str) -> str | None:
        """The guide id for a channel name; None when none or several match."""
        key = normalize_name(name)
        return self._by_name.get(key) if key else None

    def links(self, channels: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """(name, tvg_id) of live channels -> guide id, for the channels whose
        tvg-id is empty or unknown to the guide and whose name has exactly
        one guide channel."""
        out: dict[tuple[str, str], str] = {}
        for name, tvg_id in channels:
            if tvg_id and tvg_id.lower() in self.ids:
                continue
            found = self.match(name)
            if found is not None and found != tvg_id.lower():
                out[(name, tvg_id)] = found
        return out
