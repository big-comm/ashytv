"""Speed test (Settings → Network).

Download speed and latency of the internet, from Cloudflare's speed
endpoint (a few parallel streams for about ten seconds, 25 MB blocks — it
refuses bigger requests), and the response time of each playlist's server,
which causes stalls more often than the line itself.

GTK-free: the measuring functions block and run in a worker thread; the
page polls a `Meter` for the running value.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from enum import Enum
from urllib.parse import urlsplit

DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
LATENCY_URL = "https://speed.cloudflare.com/__down?bytes=0"
STREAMS = 4
DOWNLOAD_SECS = 10.0
LATENCY_TRIES = 4
SLOW_SERVER_MS = 800        # a playlist server slower than this gets a warning
_TIMEOUT = 8
_CHUNK = 64 * 1024


class Verdict(str, Enum):
    """What the line is good for (Mbit/s per stream, with some margin)."""

    UHD = "uhd"     # 4K
    FHD = "fhd"     # Full HD
    HD = "hd"
    SD = "sd"
    LOW = "low"     # too slow


def verdict(mbps: float) -> Verdict:
    if mbps >= 25:
        return Verdict.UHD
    if mbps >= 10:
        return Verdict.FHD
    if mbps >= 5:
        return Verdict.HD
    if mbps >= 3:
        return Verdict.SD
    return Verdict.LOW


def mbps(nbytes: int, seconds: float) -> float:
    return nbytes * 8 / seconds / 1e6 if seconds > 0 else 0.0


def median(values: Sequence[float]) -> float | None:
    """The median; with an even count the upper middle (the first request
    also opens the connection — DNS, TLS — and is the slow one)."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else None


def server_is_slow(ms: float | None) -> bool:
    """A server that never answered (None) counts as slow too."""
    return ms is None or ms > SLOW_SERVER_MS


def server_root(url: str) -> str:
    """'http://user:pw@host:8080/get.php?…' -> the scheme, host and port only
    (no credentials or path go into the test)."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}/"


def _user_agent() -> str:
    from .. import APP_VERSION
    return f"AshyTV/{APP_VERSION}"


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method,
                                  headers={"User-Agent": _user_agent(),
                                           "Cache-Control": "no-cache",
                                           "Accept-Encoding": "identity"})


def latency_ms(url: str, tries: int = LATENCY_TRIES,
               opener: Callable = urllib.request.urlopen,
               clock: Callable[[], float] = time.monotonic) -> float | None:
    """Median time to the answer of a small request, in ms; None when the
    server never answered. Any HTTP answer (even 404) means it responded."""
    times = []
    for _i in range(tries):
        start = clock()
        try:
            with opener(_request(url, "HEAD"), timeout=_TIMEOUT):
                pass
        except urllib.error.HTTPError:
            pass  # an answer all the same
        except (OSError, ValueError):
            continue
        times.append((clock() - start) * 1000)
    return median(times)


class Meter:
    """Bytes downloaded so far, shared by the streams (thread-safe)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.bytes = 0
        self.start = clock()

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def mbps(self) -> float:
        with self._lock:
            return mbps(self.bytes, self._clock() - self.start)


def download_mbps(meter: Meter, cancel: threading.Event | None = None,
                  url: str = DOWNLOAD_URL, streams: int = STREAMS,
                  seconds: float = DOWNLOAD_SECS,
                  opener: Callable = urllib.request.urlopen) -> float | None:
    """Download speed in Mbit/s over `streams` parallel streams for up to
    `seconds`; None when nothing could be downloaded."""
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + seconds

    def running() -> bool:
        return time.monotonic() < deadline and not cancel.is_set()

    def stream() -> None:
        # Blocks of 25 MB, one after the other, until the time is up.
        while running():
            try:
                with opener(_request(url), timeout=_TIMEOUT) as resp:
                    while running():
                        data = resp.read(_CHUNK)
                        if not data:
                            break
                        meter.add(len(data))
            except (OSError, ValueError):
                return

    workers = [threading.Thread(target=stream, daemon=True, name="ashytv-speed")
               for _i in range(streams)]
    meter.start = time.monotonic()
    for w in workers:
        w.start()
    for w in workers:
        w.join(seconds + _TIMEOUT + 2)
    if meter.bytes == 0 or cancel.is_set():
        return None
    return mbps(meter.bytes, min(time.monotonic() - meter.start, seconds))
