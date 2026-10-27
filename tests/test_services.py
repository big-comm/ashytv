"""Importer, preferences and logo loader (no network)."""

from pathlib import Path

import pytest

from ashytv.models import Channel, MediaType
from ashytv.services import importer, logos
from ashytv.services.settings import ACCENTS, DEFAULTS, Settings
from ashytv.services.storage import Storage
from ashytv.services.xtream_client import XtreamProvider

SAMPLE = Path(__file__).parent / "sample.m3u"


def test_import_m3u_twice_keeps_one_playlist(tmp_path):
    db_path = tmp_path / "t.db"
    r1 = importer.import_m3u(db_path, "List", str(SAMPLE))
    r2 = importer.import_m3u(db_path, "List", str(SAMPLE))
    assert r1.playlist_id == r2.playlist_id
    db = Storage(db_path)
    assert len(db.list_playlists()) == 1
    assert db.count() == r2.count == 5


def test_import_empty_m3u_fails(tmp_path):
    empty = tmp_path / "empty.m3u"
    empty.write_text("#EXTM3U\n")
    with pytest.raises(ValueError):
        importer.import_m3u(tmp_path / "t.db", "Empty", str(empty))


def test_import_xtream_without_keyring_stores_password(tmp_path, monkeypatch):
    items = [Channel(name="A", url="live/1.ts"),
             Channel(name="B", url="movie/2.mp4", media_type=MediaType.MOVIE)]
    monkeypatch.setattr(importer, "load_catalog", lambda _p: (items, {"exp_date": 1}))
    monkeypatch.setattr(importer.secrets, "store_password", lambda *_a: False)
    prov = XtreamProvider("http://h", "u", "p")
    r = importer.import_xtream(tmp_path / "t.db", "X", prov)
    db = Storage(tmp_path / "t.db")
    row = db.get_playlist(r.playlist_id)
    assert XtreamProvider.from_json(row["source"]).password == "p"
    assert db.playlist_meta(r.playlist_id) == {"exp_date": 1}


def test_import_xtream_with_keyring_hides_password(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "load_catalog",
                        lambda _p: ([Channel(name="A", url="live/1.ts")], {}))
    monkeypatch.setattr(importer.secrets, "store_password", lambda *_a: True)
    prov = XtreamProvider("http://h", "u", "secret")
    r1 = importer.import_xtream(tmp_path / "t.db", "X", prov)
    r2 = importer.import_xtream(tmp_path / "t.db", "X", prov)
    db = Storage(tmp_path / "t.db")
    assert r1.playlist_id == r2.playlist_id and len(db.list_playlists()) == 1
    assert "secret" not in db.get_playlist(r1.playlist_id)["source"]


def test_settings_persist_and_notify(tmp_path):
    path = tmp_path / "s.json"
    s = Settings(path)
    assert s.get("theme") == DEFAULTS["theme"]
    seen = []
    s.connect("accent", seen.append)
    s.set("accent", "purple")
    s.set("accent", "purple")  # no change: does not notify again
    assert seen == ["purple"]
    assert Settings(path).get("accent") == "purple"
    assert "purple" in ACCENTS and len(ACCENTS) == 10


def test_settings_ignore_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert Settings(path).get("volume") == DEFAULTS["volume"]


@pytest.fixture
def loader(monkeypatch, tmp_path):
    monkeypatch.setattr(logos.GLib, "idle_add", lambda fn, *a: fn(*a))
    monkeypatch.setattr(logos.http, "cache_dir", lambda _sub: tmp_path)
    ld = logos.LogoLoader()
    # No threads: run the work immediately.
    monkeypatch.setattr(ld, "_run", lambda: None)
    return ld


def test_logo_callbacks_for_same_url_are_all_called(loader, monkeypatch):
    monkeypatch.setattr(loader, "_ensure_file", lambda url: Path("/x"))
    monkeypatch.setattr(logos.GdkPixbuf.Pixbuf, "new_from_file_at_scale",
                        lambda *_a: "pixbuf")
    monkeypatch.setattr(logos, "_texture_from_pixbuf", lambda p: f"tex:{p}")
    monkeypatch.setattr(loader, "_save_thumb", lambda *_a: None)
    got = []
    loader.load("http://logo", got.append, 40)
    loader.load("http://logo", got.append, 40)  # same logo on another channel
    assert got == []  # still queued
    key = loader._next()
    loader._work(*key)
    assert got == ["tex:pixbuf", "tex:pixbuf"]
    # Afterwards it comes straight from the in-memory cache.
    loader.load("http://logo", got.append, 40)
    assert len(got) == 3


def test_failed_logo_is_not_retried_immediately(loader, monkeypatch):
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("404")

    monkeypatch.setattr(loader, "_ensure_file", boom)
    loader.load("http://bad", lambda _t: None, 40)
    loader._work(*loader._next())
    loader.load("http://bad", lambda _t: None, 40)
    assert loader._next() is None and calls == ["http://bad"]


def test_logo_queue_serves_the_screen_in_order_newest_screen_first(loader):
    for n in range(3):  # a page opens: top to bottom
        loader.load(f"http://l/{n}", lambda _t: None, 40)
    assert loader._next()[0] == "http://l/0"  # the top of the screen first
    loader._last_request -= 10  # later, the user scrolls: a new burst
    loader.load("http://l/9", lambda _t: None, 40)
    assert [loader._next()[0] for _ in range(3)] == ["http://l/9", "http://l/1",
                                                      "http://l/2"]
    assert loader._next() is None


def test_decode_retries_transient_loader_errors(monkeypatch, tmp_path):
    calls = []

    def flaky(path, w, h, keep):
        calls.append(path)
        if len(calls) < 2:
            raise logos.GLib.Error("loader i/o error")
        return "pixbuf"

    monkeypatch.setattr(logos.GdkPixbuf.Pixbuf, "new_from_file_at_scale", flaky)
    monkeypatch.setattr(logos.time, "sleep", lambda _s: None)
    assert logos._decode(tmp_path / "x.png", 40) == "pixbuf"
    assert len(calls) == 2


def test_decode_gives_up_after_attempts(monkeypatch, tmp_path):
    def broken(*_a):
        raise logos.GLib.Error("corrupt")

    monkeypatch.setattr(logos.GdkPixbuf.Pixbuf, "new_from_file_at_scale", broken)
    monkeypatch.setattr(logos.time, "sleep", lambda _s: None)
    with pytest.raises(logos.GLib.Error):
        logos._decode(tmp_path / "x.png", 40)


def test_describe_error_is_user_friendly():
    import urllib.error

    from ashytv.services import http

    not_found = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    assert "HTTP 404" in http.describe_error(not_found)
    assert "expired" in http.describe_error(not_found)
    assert "HTTP 500" in http.describe_error(
        urllib.error.HTTPError("http://x", 500, "Oops", {}, None))
    assert "reach the server" in http.describe_error(urllib.error.URLError("no route"))
    assert http.describe_error(TimeoutError()) == "The server took too long to respond."
    assert http.describe_error(ValueError("plain")) == "plain"


def test_logo_download_survives_cleared_cache(loader, monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(logos.http, "fetch_bytes_keepalive", lambda url, timeout=0: b"data")
    loader._dir = tmp_path / "logos"
    loader._dir.mkdir()
    shutil.rmtree(loader._dir)  # "Clear cache" removed the folder
    path = loader._ensure_file("http://logo/1.png")
    assert path.read_bytes() == b"data"
    assert loader._url_locks == {}  # the per-URL lock is always released
