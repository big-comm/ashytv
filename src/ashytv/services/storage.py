"""Local persistence: playlists, channels, episodes, favorites and history (SQLite).

Each thread must open its own `Storage` instance (Python's sqlite3 doesn't
share connections across threads). WAL mode lets the background import write
while the UI reads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from gi.repository import GLib

from ..models import Channel, Episode, MediaType
from .text import normalize

_SCHEMA_VERSION = 2

# Words that identify adult groups (parental controls).
ADULT_KEYWORDS = ("adult", "xxx", "+18", "18+", "porn", "hot club", "sexy")


def _data_dir() -> Path:
    base = Path(GLib.get_user_data_dir()) / "ashytv"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_path() -> Path:
    return _data_dir() / "ashytv.db"


class Storage:
    """Thin layer over SQLite."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.hide_adult = False
        self._migrate()

    def close(self) -> None:
        self.db.close()

    # ---- schema --------------------------------------------------------

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version >= _SCHEMA_VERSION:
            return
        has_v1 = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel'"
        ).fetchone() is not None

        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS playlist (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                source    TEXT NOT NULL,          -- local path, URL or xtream JSON
                kind      TEXT NOT NULL DEFAULT 'm3u',  -- m3u | xtream
                added_at  INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS channel (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                url         TEXT NOT NULL,
                media_type  TEXT NOT NULL DEFAULT 'live',
                grp         TEXT DEFAULT '',
                tvg_id      TEXT DEFAULT '',
                tvg_name    TEXT DEFAULT '',
                logo        TEXT DEFAULT '',
                favorite    INTEGER DEFAULT 0     -- legacy (v1); see the favorite table
            );
            """
        )
        self._add_columns("playlist", {"updated_at": "INTEGER DEFAULT 0",
                                       "meta": "TEXT DEFAULT '{}'"})
        self._add_columns("channel", {
            "num": "INTEGER DEFAULT 0",
            "name_key": "TEXT DEFAULT ''",
            "ext_id": "TEXT DEFAULT ''",
            "rating": "REAL DEFAULT 0",
            "year": "TEXT DEFAULT ''",
            "plot": "TEXT DEFAULT ''",
            "genre": "TEXT DEFAULT ''",
            "backdrop": "TEXT DEFAULT ''",
            "added": "INTEGER DEFAULT 0",
            "user_agent": "TEXT DEFAULT ''",
            "referrer": "TEXT DEFAULT ''",
        })
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS episode (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
                season      INTEGER NOT NULL,
                episode     INTEGER NOT NULL,
                title       TEXT NOT NULL,
                url         TEXT NOT NULL,
                logo        TEXT DEFAULT '',
                plot        TEXT DEFAULT '',
                duration    INTEGER DEFAULT 0
            );
            -- Favorites and history use (playlist, url) — a stable key that
            -- survives a "refresh playlist" (channel ids change).
            CREATE TABLE IF NOT EXISTS favorite (
                playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
                url         TEXT NOT NULL,
                added_at    INTEGER DEFAULT (strftime('%s','now')),
                PRIMARY KEY (playlist_id, url)
            );
            CREATE TABLE IF NOT EXISTS history (
                playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
                url         TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                title       TEXT NOT NULL,
                subtitle    TEXT DEFAULT '',
                logo        TEXT DEFAULT '',
                tvg_id      TEXT DEFAULT '',
                parent      TEXT DEFAULT '',      -- series (url) of an episode
                position    REAL DEFAULT 0,
                duration    REAL DEFAULT 0,
                watched_at  INTEGER NOT NULL,
                PRIMARY KEY (playlist_id, url)
            );
            DROP INDEX IF EXISTS idx_channel_type;
            CREATE INDEX IF NOT EXISTS idx_channel_playlist ON channel(playlist_id);
            CREATE INDEX IF NOT EXISTS idx_channel_type_grp
                ON channel(media_type, grp, name);
            CREATE INDEX IF NOT EXISTS idx_channel_type_num ON channel(media_type, num);
            CREATE INDEX IF NOT EXISTS idx_channel_added ON channel(media_type, added);
            CREATE INDEX IF NOT EXISTS idx_episode_channel ON episode(channel_id);
            CREATE INDEX IF NOT EXISTS idx_history_time ON history(watched_at);
            """
        )
        if has_v1:
            # Old favorites (column) -> favorite table; fills name_key.
            self.db.execute(
                """INSERT OR IGNORE INTO favorite (playlist_id, url)
                   SELECT playlist_id, url FROM channel WHERE favorite = 1""")
            rows = self.db.execute("SELECT id, name FROM channel").fetchall()
            self.db.executemany("UPDATE channel SET name_key = ? WHERE id = ?",
                                [(normalize(r["name"]), r["id"]) for r in rows])
        self.db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self.db.commit()

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---- playlists -----------------------------------------------------

    def add_playlist(self, name: str, source: str, kind: str = "m3u") -> int:
        cur = self.db.execute(
            "INSERT INTO playlist (name, source, kind) VALUES (?, ?, ?)",
            (name, source, kind),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def upsert_playlist(self, name: str, source: str, kind: str = "m3u") -> int:
        """Reuse the playlist with the same source (avoids duplicating channels)."""
        row = self.db.execute(
            "SELECT id FROM playlist WHERE kind = ? AND source = ?", (kind, source)
        ).fetchone()
        if row is None:
            return self.add_playlist(name, source, kind)
        self.db.execute("UPDATE playlist SET name = ? WHERE id = ?", (name, row["id"]))
        self.db.commit()
        return int(row["id"])

    def list_playlists(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM playlist ORDER BY added_at DESC, id DESC"
        ).fetchall()

    def get_playlist(self, playlist_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM playlist WHERE id = ?", (playlist_id,)
        ).fetchone()

    def playlist_meta(self, playlist_id: int) -> dict:
        row = self.get_playlist(playlist_id)
        try:
            return json.loads(row["meta"] or "{}") if row else {}
        except ValueError:
            return {}

    def set_playlist_meta(self, playlist_id: int, meta: dict) -> None:
        self.db.execute(
            "UPDATE playlist SET meta = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), int(time.time()), playlist_id),
        )
        self.db.commit()

    def update_playlist_meta(self, playlist_id: int, **values) -> None:
        """Merge keys into the playlist metadata without touching updated_at."""
        meta = self.playlist_meta(playlist_id)
        meta.update(values)
        self.db.execute("UPDATE playlist SET meta = ? WHERE id = ?",
                        (json.dumps(meta), playlist_id))
        self.db.commit()

    def stale_playlists(self, max_age: int, now: float | None = None) -> list[int]:
        """Playlists due for an automatic refresh, oldest first.

        A playlist whose last automatic refresh failed (e.g. an expired
        account) waits a full interval before being tried again, so it never
        blocks the others.
        """
        now = now or time.time()
        due = []
        for row in self.list_playlists():
            meta = self.playlist_meta(int(row["id"]))
            last = max(row["updated_at"] or 0, int(meta.get("refresh_failed_at", 0)))
            if now - last > max_age:
                due.append((last, int(row["id"])))
        return [pid for _last, pid in sorted(due)]

    def has_playlists(self) -> bool:
        return self.db.execute("SELECT 1 FROM playlist LIMIT 1").fetchone() is not None

    def delete_playlist(self, playlist_id: int) -> None:
        self.db.execute("DELETE FROM playlist WHERE id = ?", (playlist_id,))
        self.db.commit()

    def clear_history(self) -> None:
        self.db.execute("DELETE FROM history")
        self.db.commit()

    # ---- channels ------------------------------------------------------

    def replace_channels(
        self,
        playlist_id: int,
        channels: Iterable[Channel],
        episodes: dict[str, list[Episode]] | None = None,
    ) -> int:
        """Replace the entire contents of a playlist in a single transaction.

        `episodes` maps a series' url (key) to its episodes.
        Returns the number of items written.
        """
        episodes = episodes or {}
        with self.db:
            self.db.execute("DELETE FROM channel WHERE playlist_id = ?", (playlist_id,))
            count = 0
            series_ids: dict[str, int] = {}
            insert = """INSERT INTO channel
                (playlist_id, num, name, name_key, url, media_type, grp, tvg_id,
                 tvg_name, logo, ext_id, rating, year, plot, genre, backdrop, added,
                 user_agent, referrer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            batch = []
            for num, c in enumerate(channels, 1):
                row = (playlist_id, num, c.name, normalize(c.display_name), c.url,
                       c.media_type.value, c.group, c.tvg_id, c.tvg_name, c.logo,
                       c.ext_id, c.rating, c.year, c.plot, c.genre, c.backdrop,
                       c.added, c.user_agent, c.referrer)
                if c.url in episodes:
                    cur = self.db.execute(insert, row)
                    series_ids[c.url] = int(cur.lastrowid)
                else:
                    batch.append(row)
                    if len(batch) >= 5000:
                        self.db.executemany(insert, batch)
                        batch.clear()
                count += 1
            if batch:
                self.db.executemany(insert, batch)
            self.db.executemany(
                """INSERT INTO episode (channel_id, season, episode, title, url,
                   logo, plot, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(series_ids[key], e.season, e.episode, e.title, e.url, e.logo,
                  e.plot, e.duration)
                 for key, eps in episodes.items() if key in series_ids
                 for e in eps],
            )
            self.db.execute("UPDATE playlist SET updated_at = ? WHERE id = ?",
                            (int(time.time()), playlist_id))
        return count

    def count_channels(self, playlist_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM channel WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        return int(row["n"])

    def _where(self, media_type, favorites, group, search, groups=None):
        clauses, params = [], []
        if media_type is not None:
            clauses.append("c.media_type = ?")
            params.append(media_type.value)
        if favorites:
            clauses.append("f.url IS NOT NULL")
        if group is not None:
            clauses.append("c.grp = ?")
            params.append(group)
        if groups is not None:
            clauses.append(f"c.grp IN ({','.join('?' * len(groups))})")
            params.extend(groups)
        if search:
            clauses.append("c.name_key LIKE ?")
            params.append(f"%{normalize(search)}%")
        if self.hide_adult:
            for kw in ADULT_KEYWORDS:
                clauses.append("lower(c.grp) NOT LIKE ?")
                params.append(f"%{kw}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    _FROM = """FROM channel c LEFT JOIN favorite f
               ON f.playlist_id = c.playlist_id AND f.url = c.url"""

    def channels(
        self,
        media_type: MediaType | None = None,
        favorites: bool = False,
        group: str | None = None,
        search: str = "",
        order: str = "num",
        limit: int | None = None,
        groups: list[str] | None = None,
    ) -> list[Channel]:
        where, params = self._where(media_type, favorites, group, search, groups)
        order_sql = {
            "num": "c.playlist_id, c.num",
            "name": "c.name_key",
            "added": "c.added DESC, c.num",
            "favorite": "f.added_at DESC",
        }[order]
        sql = f"SELECT c.*, f.url IS NOT NULL AS fav {self._FROM}{where} ORDER BY {order_sql}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row_to_channel(r) for r in self.db.execute(sql, params)]

    def count(
        self,
        media_type: MediaType | None = None,
        favorites: bool = False,
        group: str | None = None,
    ) -> int:
        where, params = self._where(media_type, favorites, group, "")
        return int(self.db.execute(f"SELECT COUNT(*) {self._FROM}{where}",
                                   params).fetchone()[0])

    def groups(self, media_type: MediaType) -> list[tuple[str, int]]:
        """List of (group, count) for a category, in provider order."""
        where, params = self._where(media_type, False, None, "")
        rows = self.db.execute(
            f"""SELECT c.grp, COUNT(*) AS n, MIN(c.playlist_id * 10000000 + c.num) AS o
                {self._FROM}{where} {'AND' if where else 'WHERE'} c.grp <> ''
                GROUP BY c.grp ORDER BY o""",
            params,
        ).fetchall()
        return [(r["grp"], r["n"]) for r in rows]

    def channels_in_group(self, media_type: MediaType, group: str) -> list[Channel]:
        return self.channels(media_type=media_type, group=group)

    def get_channel(self, channel_id: int) -> Channel | None:
        row = self.db.execute(
            f"SELECT c.*, f.url IS NOT NULL AS fav {self._FROM} WHERE c.id = ?",
            (channel_id,),
        ).fetchone()
        return self._row_to_channel(row) if row else None

    def find_channel(self, playlist_id: int, url: str) -> Channel | None:
        row = self.db.execute(
            f"""SELECT c.*, f.url IS NOT NULL AS fav {self._FROM}
                WHERE c.playlist_id = ? AND c.url = ?""",
            (playlist_id, url),
        ).fetchone()
        return self._row_to_channel(row) if row else None

    # ---- favorites -----------------------------------------------------

    def set_favorite(self, channel: Channel, value: bool) -> None:
        if value:
            self.db.execute(
                "INSERT OR IGNORE INTO favorite (playlist_id, url) VALUES (?, ?)",
                (channel.playlist_id, channel.url),
            )
        else:
            self.db.execute(
                "DELETE FROM favorite WHERE playlist_id = ? AND url = ?",
                (channel.playlist_id, channel.url),
            )
        self.db.commit()
        channel.favorite = value

    # ---- episodes ------------------------------------------------------

    def episodes(self, channel_id: int) -> list[Episode]:
        rows = self.db.execute(
            "SELECT * FROM episode WHERE channel_id = ? ORDER BY season, episode",
            (channel_id,),
        ).fetchall()
        return [Episode(id=r["id"], season=r["season"], episode=r["episode"],
                        title=r["title"], url=r["url"], logo=r["logo"],
                        plot=r["plot"], duration=r["duration"]) for r in rows]

    def replace_episodes(self, channel_id: int, episodes: list[Episode]) -> None:
        with self.db:
            self.db.execute("DELETE FROM episode WHERE channel_id = ?", (channel_id,))
            self.db.executemany(
                """INSERT INTO episode (channel_id, season, episode, title, url,
                   logo, plot, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(channel_id, e.season, e.episode, e.title, e.url, e.logo, e.plot,
                  e.duration) for e in episodes],
            )

    # ---- history -------------------------------------------------------

    def record_history(
        self,
        playlist_id: int,
        url: str,
        media_type: MediaType,
        title: str,
        subtitle: str = "",
        logo: str = "",
        tvg_id: str = "",
        position: float = 0,
        duration: float = 0,
        parent: str = "",
    ) -> None:
        if not playlist_id:
            return
        self.db.execute(
            """INSERT INTO history (playlist_id, url, media_type, title, subtitle,
                   logo, tvg_id, parent, position, duration, watched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (playlist_id, url) DO UPDATE SET
                   title = excluded.title, subtitle = excluded.subtitle,
                   logo = excluded.logo, position = excluded.position,
                   duration = excluded.duration, watched_at = excluded.watched_at""",
            (playlist_id, url, media_type.value, title, subtitle, logo, tvg_id,
             parent, position, duration, int(time.time())),
        )
        self.db.commit()

    def history(self, limit: int = 20, media_type: MediaType | None = None) -> list:
        sql = "SELECT * FROM history"
        params: list = []
        if media_type is not None:
            sql += " WHERE media_type = ?"
            params.append(media_type.value)
        sql += " ORDER BY watched_at DESC LIMIT ?"
        params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def resume_position(self, playlist_id: int, url: str) -> float:
        row = self.db.execute(
            "SELECT position, duration FROM history WHERE playlist_id = ? AND url = ?",
            (playlist_id, url),
        ).fetchone()
        if row is None or not row["duration"]:
            return 0.0
        # Almost at the end? Restart from the beginning.
        if row["position"] > row["duration"] * 0.95:
            return 0.0
        return float(row["position"])

    def recent_urls(self, media_type: MediaType, limit: int = 50) -> list[str]:
        return [r["url"] for r in self.history(limit, media_type)]

    @staticmethod
    def _row_to_channel(r: sqlite3.Row) -> Channel:
        return Channel(
            id=r["id"],
            playlist_id=r["playlist_id"],
            name=r["name"],
            url=r["url"],
            media_type=MediaType(r["media_type"]),
            group=r["grp"] or "",
            tvg_id=r["tvg_id"] or "",
            tvg_name=r["tvg_name"] or "",
            logo=r["logo"] or "",
            favorite=bool(r["fav"]),
            ext_id=r["ext_id"] or "",
            rating=float(r["rating"] or 0),
            year=r["year"] or "",
            plot=r["plot"] or "",
            genre=r["genre"] or "",
            backdrop=r["backdrop"] or "",
            added=int(r["added"] or 0),
            user_agent=r["user_agent"] or "",
            referrer=r["referrer"] or "",
        )
