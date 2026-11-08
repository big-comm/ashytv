"""Writes one channel to a file, on its own, for a scheduled recording.

Not the player's recorder: that one saves what is on screen, through mpv.
This opens the channel itself, so a recording happens whether or not anyone
is watching and without moving the viewer off the channel they chose.

The stream is written exactly as it arrives — no decoding, no re-encoding.
What comes down the wire is already compressed video (H.264 or HEVC in an
MPEG-TS), so copying the bytes costs almost nothing and loses nothing;
re-encoding would cost hours of processor to save little and lose quality.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import http

# Read in pieces rather than by line: this is video, not text.
CHUNK = 256 * 1024
# A live stream that sends nothing for this long is treated as stopped.
STALL = 30
# Reopen the channel this many times if it drops before the end.
RETRIES = 5
RETRY_WAIT = 5


def safe_name(text: str, fallback: str) -> str:
    """A file name from a title: no separators, no control characters, and
    short enough that accents cannot push it past the 255-byte limit."""
    clean = re.sub(r"[^\w\s.-]", "", text or "").strip()
    clean = re.sub(r"\s+", " ", clean) or fallback
    return clean.encode()[:120].decode(errors="ignore").strip() or fallback


class Recording:
    """One recording in progress. Call stop() to end it early."""

    def __init__(self, url: str, path: Path, until: float,
                 on_done=None, on_fail=None):
        self.path = path
        self._url = url
        self._until = until
        self._on_done = on_done
        self._on_fail = on_fail
        self._stop = threading.Event()
        self.bytes = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="record")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        err = ""
        try:
            with open(self.path, "wb") as out:
                for attempt in range(RETRIES + 1):
                    if self._stop.is_set() or time.time() >= self._until:
                        break
                    try:
                        self._pull(out)
                    except Exception as exc:  # the stream dropped
                        err = http.describe_error(exc)
                        if self._stop.is_set() or time.time() >= self._until:
                            break
                        if attempt == RETRIES:
                            break
                        # A channel that drops mid-programme is normal; pick
                        # it up again and keep writing to the same file.
                        time.sleep(RETRY_WAIT)
                        continue
                    err = ""
                    if self._stop.is_set() or time.time() >= self._until:
                        break
        except OSError as exc:
            err = http.describe_error(exc)

        if self.bytes == 0:
            self._fail(err or "empty")
            return
        if self._on_done:
            self._on_done(self)

    def _pull(self, out) -> None:
        with http.open_url(self._url, timeout=STALL) as resp:
            while not self._stop.is_set() and time.time() < self._until:
                piece = resp.read(CHUNK)
                if not piece:
                    raise OSError("stream ended")
                out.write(piece)
                self.bytes += len(piece)

    def _fail(self, reason: str) -> None:
        # Nothing was written: leave no empty file behind to puzzle anyone.
        try:
            if self.path.exists() and self.path.stat().st_size == 0:
                self.path.unlink()
        except OSError:
            pass
        if self._on_fail:
            self._on_fail(self, reason)
