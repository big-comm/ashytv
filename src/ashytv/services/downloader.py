"""Asynchronous download of M3U playlists (URL or local file)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from gi.repository import GLib

from . import http


def fetch_playlist_async(
    source: str,
    on_done: Callable[[str], None],
    on_error: Callable[[Exception], None],
) -> None:
    """Load the text of an M3U (http(s) URL or local file) in the background.

    Callbacks are invoked on the main (GTK) thread via GLib.idle_add.
    """

    def worker() -> None:
        try:
            text = read_source(source)
        except Exception as exc:
            GLib.idle_add(on_error, exc)
        else:
            GLib.idle_add(on_done, text)

    threading.Thread(target=worker, daemon=True).start()


def read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        data = http.fetch_bytes(source, timeout=120)
        return data.decode("utf-8-sig", errors="replace")
    return Path(source).expanduser().read_text(encoding="utf-8-sig", errors="replace")
