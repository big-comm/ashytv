"""Storage: migration, stable favorites, history, filters and search."""

import sqlite3

from ashytv.models import Channel, Episode, MediaType
from ashytv.services.storage import Storage


def _ch(name, url, mt=MediaType.LIVE, group="G", **kw):
    return Channel(name=name, url=url, media_type=mt, group=group, **kw)


def test_favorites_survive_refresh(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("A", "u/a"), _ch("B", "u/b")])
    a = db.channels(MediaType.LIVE)[0]
    db.set_favorite(a, True)
    # "Refresh list": ids change, the favorite stays.
    db.replace_channels(pid, [_ch("A", "u/a"), _ch("B", "u/b"), _ch("C", "u/c")])
    favs = db.channels(favorites=True)
    assert [c.name for c in favs] == ["A"] and favs[0].favorite
    assert db.count(MediaType.LIVE, favorites=True) == 1


def test_upsert_playlist_does_not_duplicate(tmp_path):
    db = Storage(tmp_path / "t.db")
    p1 = db.upsert_playlist("List", "http://x/l.m3u")
    p2 = db.upsert_playlist("New list", "http://x/l.m3u")
    assert p1 == p2
    assert len(db.list_playlists()) == 1
    assert db.get_playlist(p1)["name"] == "New list"


def test_delete_playlist_cascades(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("A", "u/a")])
    db.set_favorite(db.channels()[0], True)
    db.record_history(pid, "u/a", MediaType.LIVE, "A")
    db.delete_playlist(pid)
    assert db.count() == 0
    assert db.history() == []
    assert not db.has_playlists()


def test_search_ignores_accents_and_case(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("Ação Total", "u/1", MediaType.MOVIE),
                              _ch("Comédia", "u/2", MediaType.MOVIE)])
    assert [c.name for c in db.channels(MediaType.MOVIE, search="acao")] == ["Ação Total"]
    assert [c.name for c in db.channels(MediaType.MOVIE, search="COMÉ")] == ["Comédia"]


def test_hide_adult_filters_groups(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("A", "u/1", group="Free-to-air"),
                              _ch("X", "u/2", group="ADULTOS +18")])
    assert db.count(MediaType.LIVE) == 2
    db.hide_adult = True
    assert db.count(MediaType.LIVE) == 1
    assert [g for g, _n in db.groups(MediaType.LIVE)] == ["Free-to-air"]


def test_groups_keep_provider_order(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("1", "u/1", group="Zeta"), _ch("2", "u/2", group="Alpha"),
                              _ch("3", "u/3", group="Zeta")])
    assert db.groups(MediaType.LIVE) == [("Zeta", 2), ("Alpha", 1)]


def test_history_and_resume(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.record_history(pid, "movie/1.mp4", MediaType.MOVIE, "Movie", position=120,
                      duration=6000)
    assert db.resume_position(pid, "movie/1.mp4") == 120
    db.record_history(pid, "movie/1.mp4", MediaType.MOVIE, "Movie", position=5990,
                      duration=6000)
    assert db.resume_position(pid, "movie/1.mp4") == 0  # finished: restarts
    db.record_history(pid, "series/9.mp4", MediaType.SERIES, "Show", parent="series/9")
    assert db.history(1)[0]["parent"] == "series/9"
    db.clear_history()
    assert db.history() == []


def test_episodes_roundtrip(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src", "xtream")
    db.replace_channels(pid, [_ch("Show", "series/9", MediaType.SERIES, ext_id="9")])
    sid = db.channels(MediaType.SERIES)[0].id
    db.replace_episodes(sid, [Episode(1, 2, "B", "series/2.mp4"),
                              Episode(1, 1, "A", "series/1.mp4")])
    assert [e.title for e in db.episodes(sid)] == ["A", "B"]


def test_migration_from_v1(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE playlist (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            source TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'm3u',
            added_at INTEGER DEFAULT (strftime('%s','now')));
        CREATE TABLE channel (id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL REFERENCES playlist(id) ON DELETE CASCADE,
            name TEXT NOT NULL, url TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'live',
            grp TEXT DEFAULT '', tvg_id TEXT DEFAULT '', tvg_name TEXT DEFAULT '',
            logo TEXT DEFAULT '', favorite INTEGER DEFAULT 0);
        INSERT INTO playlist (name, source) VALUES ('Old', 'x.m3u');
        INSERT INTO channel (playlist_id, name, url, favorite) VALUES (1, 'Canal Ação', 'u/1', 1);
    """)
    old.commit()
    old.close()

    db = Storage(path)
    assert db.db.execute("PRAGMA user_version").fetchone()[0] == 2
    ch = db.channels()[0]
    assert ch.favorite  # old favorite migrated to the new table
    assert db.channels(search="acao")[0].name == "Canal Ação"
    db.close()
    Storage(path).close()  # reopening does not migrate again


def test_stale_playlists_skip_recent_failures(tmp_path):
    db = Storage(tmp_path / "t.db")
    old = db.add_playlist("Expired", "a")
    good = db.add_playlist("Good", "b")
    db.db.execute("UPDATE playlist SET updated_at = 1000 WHERE id = ?", (old,))
    db.db.execute("UPDATE playlist SET updated_at = 2000 WHERE id = ?", (good,))
    db.db.commit()
    assert db.stale_playlists(max_age=100, now=10_000) == [old, good]
    # A failed automatic refresh pushes the playlist back by one interval.
    db.update_playlist_meta(old, refresh_failed_at=9_950)
    assert db.stale_playlists(max_age=100, now=10_000) == [good]
    assert db.get_playlist(old)["updated_at"] == 1000  # not marked as updated
    assert db.playlist_meta(old)["refresh_failed_at"] == 9_950
