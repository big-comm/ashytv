"""Programme guide (EPG) from XMLTV.

Supports the `xmltv.php` of Xtream providers (and any XMLTV, including
gzip-compressed). Indexes programmes by `channel` (= tvg-id) and answers
"what's on now / next" and "what's on between X and Y".
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import io
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET

from gi.repository import GLib

from . import http

# A cached guide is valid for this long before being downloaded again.
CACHE_TTL = 6 * 3600


@dataclass(slots=True)
class Programme:
    start: int          # epoch (s)
    stop: int
    title: str
    desc: str = ""

    def progress(self, now: int) -> float:
        span = max(1, self.stop - self.start)
        return min(1.0, max(0.0, (now - self.start) / span))


class EpgGuide:
    """Index of tvg-id -> list of programmes (sorted by start)."""

    def __init__(self) -> None:
        self._by_channel: dict[str, list[Programme]] = {}
        self._starts: dict[str, list[int]] = {}

    def __bool__(self) -> bool:
        return bool(self._by_channel)

    @property
    def channel_count(self) -> int:
        return len(self._by_channel)

    @property
    def programme_count(self) -> int:
        return sum(len(v) for v in self._by_channel.values())

    def has_channel(self, tvg_id: str) -> bool:
        return tvg_id in self._by_channel

    def load_xmltv(self, data) -> None:
        """Load from bytes or a binary file (stream), gzipped or not."""
        stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
        stream = _maybe_gunzip(stream)
        index: dict[str, list[Programme]] = {}
        root = None
        # iterparse to avoid loading the whole tree into memory.
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                continue
            if elem.tag == "programme":
                cid = elem.get("channel", "")
                start = _parse_ts(elem.get("start_timestamp"), elem.get("start"))
                stop = _parse_ts(elem.get("stop_timestamp"), elem.get("stop"))
                if cid and start:
                    title = (elem.findtext("title") or "").strip()
                    desc = (elem.findtext("desc") or "").strip()
                    index.setdefault(cid, []).append(
                        Programme(start=start, stop=stop or start, title=title,
                                  desc=desc))
                elem.clear()
                if root is not None:
                    root.clear()  # discard already-processed children
            elif elem.tag == "channel":
                elem.clear()
        for cid, progs in index.items():
            index[cid] = _dedupe(progs)
        self._by_channel = index
        self._starts = {cid: [p.start for p in progs] for cid, progs in index.items()}

    def now_next(
        self, tvg_id: str, now: int | None = None
    ) -> tuple[Programme | None, Programme | None]:
        progs = self._by_channel.get(tvg_id)
        if not progs:
            return None, None
        now = now or int(time.time())
        i = bisect.bisect_right(self._starts[tvg_id], now) - 1
        current = progs[i] if i >= 0 and progs[i].start <= now < progs[i].stop else None
        nxt = progs[i + 1] if i + 1 < len(progs) else None
        return current, nxt

    def between(self, tvg_id: str, start: int, end: int) -> list[Programme]:
        """Programmes that overlap the [start, end) interval."""
        progs = self._by_channel.get(tvg_id)
        if not progs:
            return []
        i = max(0, bisect.bisect_right(self._starts[tvg_id], start) - 1)
        out = []
        for p in progs[i:]:
            if p.start >= end:
                break
            if p.stop > start:
                out.append(p)
        return out

    def day_range(self) -> tuple[int, int]:
        """Earliest start and latest end of the whole guide."""
        starts = [p[0] for p in self._starts.values() if p]
        stops = [ps[-1].stop for ps in self._by_channel.values() if ps]
        if not starts:
            return 0, 0
        return min(starts), max(stops)


def _dedupe(progs: list[Programme]) -> list[Programme]:
    """Sort and remove overlaps (the same id may come from several sources)."""
    progs.sort(key=lambda p: (p.start, -p.stop))
    out: list[Programme] = []
    for p in progs:
        if out and p.start < out[-1].stop:
            continue  # overlaps the previous one: keep the first
        out.append(p)
    return out


def _maybe_gunzip(stream):
    """Detect gzip from the header (the stream must be seekable)."""
    head = stream.read(2)
    stream.seek(0)
    if head == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=stream)
    return stream


def _parse_ts(timestamp: str | None, xmltv_time: str | None) -> int:
    """Convert an epoch timestamp or XMLTV date (YYYYMMDDHHMMSS +ZZZZ) to epoch."""
    if timestamp and timestamp.isdigit():
        return int(timestamp)
    if not xmltv_time:
        return 0
    value = xmltv_time.strip()
    try:
        if len(value) > 14 and value[14:].strip():
            # With timezone: "20261023120000 +0000"
            stamp = value[:14] + " " + value[14:].strip().replace(":", "")
            return int(datetime.strptime(stamp, "%Y%m%d%H%M%S %z").timestamp())
        # No timezone: local time (XMLTV behavior).
        return int(time.mktime(time.strptime(value[:14], "%Y%m%d%H%M%S")))
    except (ValueError, OverflowError):
        return 0


def _cache_path(url: str):
    # The key doesn't store the URL (it contains the password), only the hash.
    return http.cache_dir("epg") / (hashlib.sha256(url.encode()).hexdigest()[:24]
                                    + ".xml")


def fetch_guide_async(url, on_done, on_error=None, max_age: int = CACHE_TTL) -> None:
    """Load the XMLTV (disk cache or network) in the background.

    on_done(guide) and on_error(exc) are called on the main thread.
    """

    def worker() -> None:
        path = _cache_path(url)
        try:
            fresh = path.exists() and time.time() - path.stat().st_mtime < max_age
            if not fresh:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
                with http.open_url(url, timeout=120) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 16):
                        out.write(chunk)
                tmp.replace(path)
            guide = EpgGuide()
            with open(path, "rb") as fh:
                guide.load_xmltv(fh)
        except Exception as exc:
            if on_error:
                GLib.idle_add(on_error, exc)
            return
        GLib.idle_add(on_done, guide)

    threading.Thread(target=worker, daemon=True).start()
