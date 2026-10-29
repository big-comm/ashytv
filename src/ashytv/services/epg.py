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
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET

from gi.repository import GLib

from . import http

# A cached guide is valid for this long before being downloaded again.
CACHE_TTL = 6 * 3600
# Programmes kept around "now" (the guide page shows yesterday to next week).
KEEP_BEFORE = 2 * 86400
KEEP_AFTER = 8 * 86400
# A programme without a stop time that is also the last of its channel.
DEFAULT_LENGTH = 3600


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
        return tvg_id.lower() in self._by_channel

    def load_xmltv(self, data, channels: set[str] | None = None,
                   now: int | None = None) -> None:
        """Load from bytes or a binary file (stream), gzipped or not.

        `channels` (lowercase tvg-ids) keeps only the channels of the user's
        playlists; programmes far from `now` are dropped too. Big guides
        carry thousands of channels and weeks of programmes: keeping them all
        cost hundreds of MB.
        """
        stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
        stream = _maybe_gunzip(stream)
        index: dict[str, list[Programme]] = {}
        lo = hi = 0
        if now is not None:
            lo, hi = now - KEEP_BEFORE, now + KEEP_AFTER
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
                wanted = channels is None or cid.lower() in channels
                in_window = not hi or (start < hi and (stop or start) > lo)
                if cid and start and wanted and in_window:
                    title = (elem.findtext("title") or "").strip()
                    desc = (elem.findtext("desc") or "").strip()
                    # stop=0 (missing) is filled in by _dedupe from the next start.
                    # Case-insensitive ids: playlists and guides disagree often.
                    index.setdefault(cid.lower(), []).append(
                        Programme(start=start, stop=stop, title=title, desc=desc))
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
        tvg_id = tvg_id.lower()
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
        tvg_id = tvg_id.lower()
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
    """Sort and fix overlaps and missing stop times.

    The same id may come from several sources: a programme that overlaps the
    previous one is a duplicate and is dropped. But when the previous one
    covers it entirely, the previous one has a wrong stop time (a common
    guide error, e.g. "until midnight") and is cut instead — otherwise it
    would hide every programme after it. A missing stop is the next start.
    """
    progs.sort(key=lambda p: (p.start, -p.stop))
    out: list[Programme] = []
    for p in progs:
        prev = out[-1] if out else None
        if prev is not None:
            if p.start == prev.start:
                continue
            if prev.stop <= prev.start:
                prev.stop = p.start  # missing stop
            elif p.start < prev.stop:
                if p.stop and prev.stop >= p.stop:
                    prev.stop = p.start  # the previous one swallowed this one
                else:
                    continue  # overlapping duplicate: keep the first
        out.append(p)
    if out and out[-1].stop <= out[-1].start:
        out[-1].stop = out[-1].start + DEFAULT_LENGTH
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
    m = _XMLTV_TIME_RE.match(xmltv_time)
    if not m:
        return 0
    # Shorter dates ("YYYYMMDDHHMM") are valid XMLTV: pad the missing fields.
    digits = m.group(1).ljust(14, "0")
    zone = (m.group(2) or "").upper()
    if zone in ("Z", "UTC", "GMT"):
        zone = "+0000"
    try:
        if zone:
            stamp = digits + " " + zone.replace(":", "")
            return int(datetime.strptime(stamp, "%Y%m%d%H%M%S %z").timestamp())
        # No timezone: local time (XMLTV behavior).
        return int(time.mktime(time.strptime(digits, "%Y%m%d%H%M%S")))
    except (ValueError, OverflowError):
        return 0


_XMLTV_TIME_RE = re.compile(r"\s*(\d{8,14})\s*([+-]\d{2}:?\d{2}|Z|UTC|GMT)?", re.I)


def _cache_path(url: str):
    # The key doesn't store the URL (it contains the password), only the hash.
    return http.cache_dir("epg") / (hashlib.sha256(url.encode()).hexdigest()[:24]
                                    + ".xml")


def download_guide(url: str, path, max_age: int = CACHE_TTL) -> None:
    """Refresh the cached XMLTV at `path` when it is older than max_age.

    The download goes to a temporary file that is always removed; a failed
    or truncated download never replaces the cache. When the network fails
    and an older copy exists, the older copy is kept (and used).
    """
    if path.exists() and time.time() - path.stat().st_mtime < max_age:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
    try:
        with http.open_url(url, timeout=120) as resp, open(tmp, "wb") as out:
            expected = int(resp.headers.get("Content-Length") or 0)
            written = 0
            while chunk := resp.read(1 << 16):
                out.write(chunk)
                written += len(chunk)
        if expected and written < expected and not resp.headers.get("Content-Encoding"):
            raise OSError(f"guide download truncated ({written} of {expected} bytes)")
        tmp.replace(path)
    except Exception:
        if not path.exists():
            raise
        # Stale beats nothing: keep the old guide, try again next time.
    finally:
        tmp.unlink(missing_ok=True)


def fetch_guide_async(url, on_done, on_error=None, max_age: int = CACHE_TTL,
                      channels: set[str] | None = None) -> None:
    """Load the XMLTV (disk cache or network) in the background.

    on_done(guide) and on_error(exc) are called on the main thread.
    `channels`: lowercase tvg-ids to keep (None keeps every channel).
    """

    def worker() -> None:
        path = _cache_path(url)
        try:
            download_guide(url, path, max_age)
            guide = EpgGuide()
            try:
                with open(path, "rb") as fh:
                    guide.load_xmltv(fh, channels, int(time.time()))
            except Exception:
                # A broken file must not stay "fresh" for hours.
                path.unlink(missing_ok=True)
                raise
        except Exception as exc:
            if on_error:
                GLib.idle_add(on_error, exc)
            return
        GLib.idle_add(on_done, guide)

    threading.Thread(target=worker, daemon=True).start()
