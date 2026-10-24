"""Background import/refresh of playlists.

Download, parsing and SQLite writes happen in a thread with its own
connection; the UI only receives the result (playlist_id, count) at the end.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import GLib

from ..i18n import _
from . import secrets
from .downloader import read_source
from .m3u_parser import group_series, header_epg_url, iter_m3u
from .storage import Storage
from .xtream_client import XtreamProvider, load_catalog


@dataclass(slots=True)
class ImportResult:
    playlist_id: int
    count: int
    kind: str
    meta: dict = field(default_factory=dict)


def import_m3u(db_path: Path, name: str, source: str,
               progress: Callable[[str], None] = lambda _m: None) -> ImportResult:
    progress(_("Downloading playlist…"))
    text = read_source(source)
    progress(_("Processing playlist…"))
    epg_url = header_epg_url(text)
    channels = list(iter_m3u(text))
    del text
    if not channels:
        raise ValueError(_("No channels found in the playlist"))
    items, episodes = group_series(channels)
    db = Storage(db_path)
    try:
        pid = db.upsert_playlist(name, source, "m3u")
        progress(_("Saving…"))
        count = db.replace_channels(pid, items, episodes)
        meta = {"epg_url": epg_url} if epg_url else {}
        db.set_playlist_meta(pid, meta)
    finally:
        db.close()
    return ImportResult(pid, count, "m3u", meta)


def import_xtream(db_path: Path, name: str, provider: XtreamProvider,
                  progress: Callable[[str], None] = lambda _m: None) -> ImportResult:
    progress(_("Connecting to provider…"))
    channels, meta = load_catalog(provider)
    if not channels:
        raise ValueError(_("The provider returned no content"))
    # Password in the system keyring; without a keyring, it is stored with the playlist.
    in_keyring = secrets.store_password(provider.host, provider.username,
                                        provider.password)
    source = provider.to_json(include_password=not in_keyring)
    key = XtreamProvider.source_key(source)
    db = Storage(db_path)
    try:
        pid = 0
        for row in db.list_playlists():
            if row["kind"] == "xtream" and XtreamProvider.source_key(row["source"]) == key:
                pid = int(row["id"])
                db.db.execute("UPDATE playlist SET name = ?, source = ? WHERE id = ?",
                              (name, source, pid))
                db.db.commit()
                break
        if not pid:
            pid = db.add_playlist(name, source, "xtream")
        progress(_("Saving…"))
        count = db.replace_channels(pid, channels)
        db.set_playlist_meta(pid, meta)
    finally:
        db.close()
    return ImportResult(pid, count, "xtream", meta)


def run_async(job: Callable[[Callable[[str], None]], ImportResult],
              on_done: Callable[[ImportResult], None],
              on_error: Callable[[Exception], None],
              on_progress: Callable[[str], None] | None = None) -> None:
    """Run `job(progress)` in a thread; callbacks run on the main thread."""

    def progress(msg: str) -> None:
        if on_progress is not None:
            GLib.idle_add(_once(on_progress), msg)

    def worker() -> None:
        try:
            result = job(progress)
        except Exception as exc:
            GLib.idle_add(_once(on_error), exc)
        else:
            GLib.idle_add(_once(on_done), result)

    threading.Thread(target=worker, daemon=True).start()


def _once(callback: Callable) -> Callable:
    """Adapt a callback for idle_add (always removes the source)."""

    def wrapper(*args):
        callback(*args)
        return GLib.SOURCE_REMOVE

    return wrapper
