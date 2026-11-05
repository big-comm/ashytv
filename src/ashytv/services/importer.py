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
from . import free_tv, secrets
from .downloader import read_source
from .failover import ServerPool
from .m3u_parser import group_series, header_epg_url, iter_m3u, looks_like_m3u
from .storage import Storage
from ..models import Channel
from .xtream_client import XtreamProvider, category_order, load_catalog, parse_provider


@dataclass(slots=True)
class ImportResult:
    playlist_id: int
    count: int
    kind: str
    meta: dict = field(default_factory=dict)


def _read_m3u(source: str) -> str:
    text = read_source(source)
    # An expired account often answers with an HTML page and HTTP 200: never
    # let that replace a good list (and with backup URLs, try the next one).
    if not looks_like_m3u(text):
        raise ValueError(_("The address did not return an M3U playlist"))
    return text


# Playlist metadata set by the user (or computed from the guide), not by the
# playlist file: kept when the playlist is imported again.
KEPT_META = ("epg_override", "epg_coverage")


def import_m3u(db_path: Path, name: str, source: str,
               progress: Callable[[str], None] = lambda _m: None,
               backups: list[str] | None = None, active: int = 0,
               epg_override: str | None = None) -> ImportResult:
    """`backups`: other URLs of the same list, tried when `source` fails.
    `epg_override`: the guide link typed by the user (wins over url-tvg)."""
    progress(_("Downloading playlist…"))
    pool = ServerPool([source, *(backups or [])], active)
    if free_tv.is_free_tv(source):
        text = free_tv.build_playlist(source, read_source)
        if not looks_like_m3u(text):
            raise ValueError(_("The address did not return an M3U playlist"))
    else:
        text = pool.run(_read_m3u)
    progress(_("Processing playlist…"))
    epg_url = header_epg_url(text)
    channels = list(iter_m3u(text))
    if free_tv.is_free_tv(source):
        free_tv.localize_groups(channels)
    else:
        _provider_group_order(pool.current, channels)
    del text
    if not channels:
        raise ValueError(_("No channels found in the playlist"))
    items, episodes = group_series(channels)
    db = Storage(db_path)
    try:
        pid = db.upsert_playlist(name, source, "m3u")
        if backups is not None:
            db.set_servers(pid, backups, pool.active)
        progress(_("Saving…"))
        count = db.replace_channels(pid, items, episodes)
        meta = {"epg_url": epg_url} if epg_url else {}
        # What the user set on the playlist survives a refresh.
        meta.update({k: v for k, v in db.playlist_meta(pid).items() if k in KEPT_META})
        if epg_override:
            meta["epg_override"] = epg_override
        db.set_playlist_meta(pid, meta)
    finally:
        db.close()
    return ImportResult(pid, count, "m3u", meta)


def _provider_group_order(source: str, channels: list[Channel]) -> None:
    """An M3U link of an Xtream server (get.php) comes A–Z by channel, so its
    groups would come in the order their first channel appears: ask the
    server for its own category order (the file order stays without it)."""
    if "get.php" not in source.lower():
        return
    provider = parse_provider(source)
    if provider is None:
        return
    order = category_order(provider)
    for media_type, names in order.items():
        rank: dict[str, int] = {}
        for name in names:
            rank.setdefault(name, len(rank) + 1)
        for c in channels:
            if c.media_type is media_type:
                c.group_rank = rank.get(c.group, 0)


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
        db.set_servers(pid, provider.backups, provider.active)
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
