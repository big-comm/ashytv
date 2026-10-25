"""M3U/M3U8 playlist parser (extended #EXTINF format)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from ..models import Channel, Episode, MediaType
from .text import episode_title

# Captures key="value" attributes inside the #EXTINF line.
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

# "Series Name S01E02" / "Nome.S1.E2" / "Nome - s01 e02"
_EPISODE_RE = re.compile(r"^(?P<show>.*?)[\s._-]*S(?P<s>\d{1,3})[\s._-]*E(?P<e>\d{1,4})\b",
                         re.IGNORECASE)

# Keywords to classify by group-title (last resort); provider group names are often Portuguese.
_MOVIE_HINTS = ("filme", "movie", "vod", "cinema")
_SERIES_HINTS = ("serie", "série", "series", "temporada", "season", "tv show")
# 24h/live channel groups that mention movies/series in their name.
_LIVE_HINTS = ("24h", "24 h", "canal", "canais", "channel", "ao vivo", "live")


def _classify(group: str, url: str = "") -> MediaType:
    # The URL path (Xtream pattern) is the most reliable source.
    path = url.split("?", 1)[0].lower()
    if "/series/" in path:
        return MediaType.SERIES
    if "/movie/" in path:
        return MediaType.MOVIE
    if "/live/" in path:
        return MediaType.LIVE

    g = group.casefold().strip()
    # "SERIES - DRAMA" / "FILMES - AÇÃO": the prefix rules.
    if g.startswith(_SERIES_HINTS):
        return MediaType.SERIES
    if g.startswith(_MOVIE_HINTS):
        return MediaType.MOVIE
    if any(h in g for h in _LIVE_HINTS):
        return MediaType.LIVE
    if any(h in g for h in _SERIES_HINTS):
        return MediaType.SERIES
    if any(h in g for h in _MOVIE_HINTS):
        return MediaType.MOVIE
    return MediaType.LIVE


def parse_m3u(text: str) -> list[Channel]:
    """Convert M3U content into a list of Channel."""
    return list(iter_m3u(text))


def iter_m3u(text: str | Iterable[str]) -> Iterator[Channel]:
    lines = text.splitlines() if isinstance(text, str) else text
    pending: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF"):
            pending = _parse_extinf(line)
        elif line.startswith("#"):
            if pending is None:
                continue
            if line.startswith("#EXTGRP:"):
                pending["group"] = line.split(":", 1)[1].strip()
            elif line.startswith("#EXTVLCOPT:"):
                key, _, value = line[len("#EXTVLCOPT:"):].partition("=")
                key = key.strip().lower()
                if key == "http-user-agent":
                    pending["user-agent"] = value.strip()
                elif key == "http-referrer":
                    pending["referrer"] = value.strip()
        else:
            # Stream URL.
            if pending is None:
                pending = {"name": line, "group": ""}
            # "Entertainment;Movies": several categories, keep the first.
            group = pending.get("group", "").split(";", 1)[0].strip()
            yield Channel(
                name=pending.get("name", ""),
                url=line,
                media_type=_classify(group, line),
                group=group,
                tvg_id=pending.get("tvg-id", ""),
                tvg_name=pending.get("tvg-name", ""),
                logo=pending.get("tvg-logo", ""),
                user_agent=pending.get("user-agent", ""),
                referrer=pending.get("referrer", ""),
            )
            pending = None


def _parse_extinf(line: str) -> dict:
    """Extract attributes and name from an #EXTINF line.

    The name comes after the first comma *outside quotes* — titles like
    "Fast, Furious 7" contain a comma and must not be cut.
    """
    in_quotes = False
    split_at = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            split_at = i
            break
    header = line if split_at < 0 else line[:split_at]
    attrs = dict(_ATTR_RE.findall(header))
    attrs["name"] = line[split_at + 1:].strip() if split_at >= 0 else ""
    attrs["group"] = attrs.get("group-title", "")
    return attrs


def header_epg_url(text: str) -> str:
    """Guide URL declared in the header: #EXTM3U url-tvg="..." / x-tvg-url."""
    for raw in text[:4096].splitlines():
        line = raw.strip()
        if line.startswith("#EXTM3U"):
            attrs = dict(_ATTR_RE.findall(line))
            url = attrs.get("x-tvg-url") or attrs.get("url-tvg") or ""
            # Some playlists carry several comma-separated URLs.
            return url.split(",")[0].strip()
        if line:
            break
    return ""


def split_episode(name: str) -> tuple[str, int, int] | None:
    """'Show S02E05' -> ('Show', 2, 5); None if it doesn't look like an episode."""
    m = _EPISODE_RE.match(name)
    if not m or not m.group("show").strip(" -._"):
        return None
    return m.group("show").strip(" -._"), int(m.group("s")), int(m.group("e"))


def group_series(
    channels: Iterable[Channel],
) -> tuple[list[Channel], dict[str, list[Episode]]]:
    """Group loose M3U episodes into series.

    Returns (items, episodes by series key). Each series becomes a single
    Channel (MediaType.SERIES, url = key ``m3u-series:<group>/<name>``) and the
    episodes go in the dictionary. Non-series items pass through untouched.
    """
    items: list[Channel] = []
    series: dict[str, Channel] = {}
    episodes: dict[str, list[Episode]] = {}
    for ch in channels:
        if ch.media_type is not MediaType.SERIES:
            items.append(ch)
            continue
        parsed = split_episode(ch.display_name)
        show, season, number = parsed if parsed else (ch.display_name, 1, 1)
        key = f"m3u-series:{ch.group}/{show}"
        if key not in series:
            series[key] = Channel(name=show, url=key, media_type=MediaType.SERIES,
                                  group=ch.group, logo=ch.logo)
            items.append(series[key])
        episodes.setdefault(key, []).append(Episode(
            season=season, episode=number,
            title=episode_title(ch.display_name, show, number), url=ch.url,
            logo=ch.logo))
    for eps in episodes.values():
        eps.sort(key=lambda e: (e.season, e.episode))
    return items, episodes
