"""Backup servers (extra DNS of the same provider) and anti-freeze playback.

A playlist may have up to MAX_BACKUPS extra base URLs besides its main one.
Requests try the server that worked last first ("sticky"), then the others in
order; they move on only when the *server* failed (network error, timeout,
HTTP 5xx, an answer that isn't what was asked for) — never when the provider
refused the login, since every server shares the same accounts.

`StallGuard` decides what to do when playback freezes: reconnect once
silently, and if it freezes again soon, try the next server.

GTK-free: safe in worker threads and in tests.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlencode, urlsplit

T = TypeVar("T")

MAX_BACKUPS = 4
SLOW_MS = 1500          # a health check slower than this is "slow"
CHECK_TIMEOUT = 8.0
CHECK_EVERY = 15 * 60   # seconds between health checks while the app is open

ONLINE, SLOW, DOWN = "online", "slow", "down"


class AuthFailed(Exception):
    """The provider refused the credentials: other servers would too."""


def is_server_failure(exc: BaseException) -> bool:
    """True when another server may succeed where this one failed."""
    if isinstance(exc, AuthFailed):
        return False
    if isinstance(exc, urllib.error.HTTPError):
        # 401/403: credentials (same on every server); other 4xx: a wrong
        # path the next server would not fix either... except 404/410 from a
        # dead DNS that now points at a parking page.
        return exc.code >= 500 or exc.code in (404, 410, 429)
    # URLError, timeouts, refused/reset connections, TLS errors, truncated
    # bodies, and garbage (JSON/M3U that doesn't parse) all mean "this server".
    return isinstance(exc, (OSError, http.client.HTTPException, ValueError, EOFError))


def clean_url(url: str) -> str:
    # Same default scheme as the main server field (imported here: the
    # Xtream client imports this module).
    from .xtream_client import normalize_host

    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = normalize_host(url)
    parts = urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.hostname else ""


def clean_backups(urls, main: str = "") -> list[str]:
    """Valid, distinct backup URLs (not the main one), at most MAX_BACKUPS."""
    seen = {clean_url(main)} if main else set()
    out = []
    for url in urls or []:
        url = clean_url(str(url))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out[:MAX_BACKUPS]


class ServerPool:
    """The servers of one playlist: main first, then the backups."""

    def __init__(self, servers: list[str], active: int = 0):
        self.servers = [s for s in servers if s] or [""]
        self.active = active if 0 <= active < len(self.servers) else 0
        self._lock = threading.Lock()

    @property
    def current(self) -> str:
        return self.servers[self.active]

    @property
    def on_backup(self) -> bool:
        return self.active != 0

    def order(self) -> list[int]:
        """The working server first, then the others in their order."""
        return [self.active] + [i for i in range(len(self.servers)) if i != self.active]

    def next_after(self, index: int) -> int | None:
        """The server to try after `index` failed (None: none left)."""
        order = self.order()
        pos = order.index(index) if index in order else -1
        return order[pos + 1] if pos + 1 < len(order) else None

    def switch_to(self, index: int) -> bool:
        with self._lock:
            changed = index != self.active
            self.active = index
            return changed

    def run(self, call: Callable[[str], T],
            on_switch: Callable[[int, int], None] | None = None) -> T:
        """call(server) on each server until one answers. The one that did
        becomes the active server (on_switch(old, new) when it changed)."""
        last: BaseException | None = None
        start = self.active
        for index in self.order():
            try:
                result = call(self.servers[index])
            except Exception as exc:
                if not is_server_failure(exc):
                    raise
                last = exc
                continue
            if self.switch_to(index) and on_switch is not None:
                on_switch(start, index)
            return result
        assert last is not None
        raise last


# ---- health -------------------------------------------------------------------

@dataclass(slots=True)
class Health:
    url: str
    status: str = ""        # online | slow | down | "" (never checked)
    latency_ms: int = 0
    checked: float = 0.0

    def to_dict(self) -> dict:
        return {"url": self.url, "status": self.status, "latency_ms": self.latency_ms,
                "checked": self.checked}

    @classmethod
    def from_dict(cls, d: dict) -> Health:
        return cls(url=str(d.get("url", "")), status=str(d.get("status", "")),
                   latency_ms=int(d.get("latency_ms", 0) or 0),
                   checked=float(d.get("checked", 0) or 0))


def classify(latency_ms: int, ok: bool) -> str:
    if not ok:
        return DOWN
    return SLOW if latency_ms > SLOW_MS else ONLINE


def check(url: str, probe: Callable[[str], None], clock=time.monotonic,
          now=time.time) -> Health:
    """probe(url) raises when the server fails; times it."""
    started = clock()
    try:
        probe(url)
        ok = True
    except Exception as exc:
        # A refused login still means the server answered.
        ok = not is_server_failure(exc)
    ms = int((clock() - started) * 1000)
    return Health(url=url, status=classify(ms, ok), latency_ms=ms if ok else 0,
                  checked=now())


def xtream_probe(username: str, password: str, fetch: Callable[[str], bytes]):
    """Light check of an Xtream server: player_api.php without an action
    answers with the account info (a few hundred bytes)."""
    def probe(server: str) -> None:
        body = fetch(f"{server}/player_api.php?"
                     + urlencode({"username": username, "password": password}))
        data = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or "user_info" not in data:
            raise ValueError("not an Xtream answer")
    return probe


def m3u_probe(fetch_head: Callable[[str], bytes]):
    """Light check of an M3U URL: its first bytes must look like a playlist."""
    def probe(url: str) -> None:
        head = fetch_head(url).lstrip(b"\xef\xbb\xbf \r\n\t")
        if not head.upper().startswith(b"#EXTM3U"):
            raise ValueError("not an M3U playlist")
    return probe


def health_list(stored, servers: list[str]) -> list[Health]:
    """Stored health (meta JSON) for the current server list, in its order."""
    by_url = {}
    for d in stored or []:
        if isinstance(d, dict):
            h = Health.from_dict(d)
            by_url[h.url] = h
    return [by_url.get(url, Health(url=url)) for url in servers]


# ---- anti-freeze ----------------------------------------------------------------

RECONNECT = "reconnect"
NEXT_SERVER = "next-server"

STALL_SECS = 8.0         # frozen this long: act
REPEAT_WINDOW = 120.0    # a second freeze within this window: next server
NO_PROGRESS_SECS = 4.0   # "playing" but the clock doesn't move: frozen too


class StallGuard:
    """Turns playback states into recovery actions (RECONNECT / NEXT_SERVER).

    The player calls `update(now, stalled)` about once a second and
    `failed(now)` when the stream errors or a live stream ends.
    """

    def __init__(self, stall_secs: float = STALL_SECS, window: float = REPEAT_WINDOW):
        self.stall_secs = stall_secs
        self.window = window
        self._since: float | None = None
        self._reconnects: list[float] = []

    def new_stream(self) -> None:
        """Another channel/movie: forget everything."""
        self._since = None
        self._reconnects.clear()

    def restarted(self) -> None:
        """The same stream was reopened (by us or a quality change)."""
        self._since = None

    def update(self, now: float, stalled: bool) -> str | None:
        if not stalled:
            self._since = None
            return None
        if self._since is None:
            self._since = now
            return None
        if now - self._since < self.stall_secs:
            return None
        self._since = None
        return self._escalate(now)

    def failed(self, now: float) -> str:
        return self._escalate(now)

    def _escalate(self, now: float) -> str:
        recent = [t for t in self._reconnects if now - t < self.window]
        if recent:
            self._reconnects.clear()
            return NEXT_SERVER
        self._reconnects = [now]
        return RECONNECT


def swap_server(url: str, old: str, new: str) -> str:
    """A stream URL built on `old` rebuilt on `new` (same path)."""
    if old and url.startswith(old + "/"):
        return new + url[len(old):]
    return url
