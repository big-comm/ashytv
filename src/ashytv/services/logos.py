"""Efficient logo loading: bounded pool, disk and memory cache.

Avoids the slowness of opening a thread per logo and re-decoding the image on
every scroll: downloads with few workers, stores on disk and keeps already
decoded (scaled) textures in memory.

Images are fetched at the size they are shown when the host offers sizes
(TMDB), and each decoded size is kept on disk as a small PNG thumbnail: the
next time the app opens, posters load straight from it, without decoding the
original again.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

from . import http  # noqa: E402

_MAX_CACHE = 1500        # textures kept in memory (LRU)
_FAIL_RETRY = 10 * 60    # don't retry a failed logo for 10 min
_WORKERS = 6
_DECODE_ATTEMPTS = 3
# gdk-pixbuf may decode through sandboxed glycin loaders (one process per
# image); too many at once makes some of them fail. Downloads stay parallel,
# decoding is limited to a couple at a time.
_DECODERS = threading.BoundedSemaphore(2)

# TMDB serves every image in these widths: /t/p/w342/<file>.
_TMDB_RE = re.compile(r"^(https?://image\.tmdb\.org/t/p/)(?:w\d+|original)(/.+)$")
_TMDB_WIDTHS = (92, 154, 185, 342, 500, 780, 1280)


def sized_url(url: str, size: int) -> str:
    """The smallest version of the image that still covers `size` pixels."""
    m = _TMDB_RE.match(url)
    if not m:
        return url
    width = next((w for w in _TMDB_WIDTHS if w >= size), None)
    return f"{m.group(1)}{f'w{width}' if width else 'original'}{m.group(2)}"


class LogoLoader:
    def __init__(self) -> None:
        # LIFO queue: the most recent request (the one that just appeared on
        # screen) is served first; those that already left the screen wait.
        self._queue: deque[tuple[str, int]] = deque()
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._dir = http.cache_dir("logos")
        self._thumbs = http.cache_dir("thumbs")
        self._tex: OrderedDict[tuple[str, int], Gdk.Texture] = OrderedDict()
        # key -> waiting callbacks (all are called when done).
        self._waiting: dict[tuple[str, int], list[Callable]] = {}
        self._failed: dict[str, float] = {}
        self._url_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.enabled = True

    def cached_texture(self, url: str, size: int = 96):
        """Already-decoded texture (instant access on bind)."""
        key = (url, size)
        tex = self._tex.get(key)
        if tex is not None:
            self._tex.move_to_end(key)
        return tex

    def load(self, url: str, callback: Callable, size: int = 96) -> None:
        """Ensure the texture and call callback(texture) on the main thread."""
        if not url or not self.enabled:
            return
        key = (url, size)
        tex = self._tex.get(key)
        if tex is not None:
            callback(tex)
            return
        failed_at = self._failed.get(url)
        if failed_at and time.monotonic() - failed_at < _FAIL_RETRY:
            return
        waiting = self._waiting.get(key)
        if waiting is not None:
            waiting.append(callback)  # already loading: just wait along
            return
        self._waiting[key] = [callback]
        with self._cond:
            self._queue.append(key)
            self._cond.notify()
        if len(self._threads) < _WORKERS:
            t = threading.Thread(target=self._run, daemon=True, name="logo")
            self._threads.append(t)
            t.start()

    def clear_memory(self) -> None:
        self._tex.clear()
        self._failed.clear()

    # ---- internal ------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                url, size = self._queue.pop()  # LIFO
            self._work(url, size)

    def _work(self, url: str, size: int) -> None:
        thumb = self.thumb_for(url, size)
        if thumb.exists():
            try:
                # Thread-safe and fast: GTK's own PNG loader, no glycin.
                texture = Gdk.Texture.new_from_filename(str(thumb))
                GLib.idle_add(self._finish, url, size, texture)
                return
            except GLib.Error:
                thumb.unlink(missing_ok=True)
        fetch = sized_url(url, size)
        try:
            path = self._ensure_file(fetch)
            pixbuf = _decode(path, size)
        except Exception:
            GLib.idle_add(self._fail, url, size)
            return
        self._save_thumb(pixbuf, thumb)
        GLib.idle_add(self._finish, url, size, _texture_from_pixbuf(pixbuf))

    def path_for(self, url: str) -> Path:
        return self._dir / hashlib.sha256(url.encode()).hexdigest()[:24]

    def thumb_for(self, url: str, size: int) -> Path:
        return self._thumbs / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}-{size}.png"

    @staticmethod
    def _save_thumb(pixbuf, thumb: Path) -> None:
        try:
            thumb.parent.mkdir(parents=True, exist_ok=True)  # "Clear cache" removes it
            tmp = thumb.with_name(thumb.name + f".{threading.get_ident()}.part")
            pixbuf.savev(str(tmp), "png", [], [])
            tmp.replace(thumb)
        except (OSError, GLib.Error):
            pass  # only a speed-up: the image is shown anyway
    def _ensure_file(self, url: str) -> Path:
        path = self.path_for(url)
        # Different sizes of the same logo don't download in parallel.
        with self._locks_guard:
            lock = self._url_locks.setdefault(url, threading.Lock())
        try:
            with lock:
                if not path.exists():
                    data = http.fetch_bytes(url, timeout=20)
                    # The folder may have been removed by "Clear cache".
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(path.name + f".{threading.get_ident()}.part")
                    tmp.write_bytes(data)
                    tmp.replace(path)  # atomic write: never leaves a half-written file
        finally:
            with self._locks_guard:
                self._url_locks.pop(url, None)
        return path

    def _fail(self, url: str, size: int) -> bool:
        self._failed[url] = time.monotonic()
        self._waiting.pop((url, size), None)
        # An invalid file (e.g. an HTML error page) must not stay in the cache.
        path = self.path_for(sized_url(url, size))
        try:
            if path.exists() and GdkPixbuf.Pixbuf.get_file_info(str(path))[0] is None:
                path.unlink()
        except (OSError, GLib.Error):
            pass
        return GLib.SOURCE_REMOVE

    def _finish(self, url: str, size: int, texture: Gdk.Texture) -> bool:
        key = (url, size)
        self._tex[key] = texture
        self._tex.move_to_end(key)
        while len(self._tex) > _MAX_CACHE:
            self._tex.popitem(last=False)
        for callback in self._waiting.pop(key, []):
            callback(texture)
        return GLib.SOURCE_REMOVE


def _decode(path: Path, size: int):
    """Decode and scale an image, retrying transient loader errors."""
    for attempt in range(_DECODE_ATTEMPTS):
        try:
            with _DECODERS:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)
        except GLib.Error:
            if attempt == _DECODE_ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable")


def _texture_from_pixbuf(pixbuf) -> Gdk.Texture:
    fmt = (Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha()
           else Gdk.MemoryFormat.R8G8B8)
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


# Single shared instance.
logo_loader = LogoLoader()
