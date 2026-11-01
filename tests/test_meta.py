"""Cast data: META API client (cache, fallbacks) and matching to the catalog."""

import io
import json
import sqlite3
import urllib.error

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from ashytv.models import Channel, MediaType  # noqa: E402
from ashytv.services import meta  # noqa: E402
from ashytv.services.meta import (  # noqa: E402
    CatalogIndex,
    Credit,
    MetaClient,
    MetaNotFound,
    MetaUnavailable,
)
from ashytv.services.storage import Storage  # noqa: E402
from ashytv.services.xtream_client import movies, parse_series_meta, parse_vod_info  # noqa: E402
from ashytv.ui.marquee import marquee_offset  # noqa: E402

MOVIE, SERIES = MediaType.MOVIE, MediaType.SERIES

TITLE = {"tmdb_id": 603, "type": "movie", "title": "Demo", "cast": [
    {"id": 6384, "name": "Ana Bright", "character": "Neo", "profile": "/abc.jpg"},
    {"id": "x", "name": "Leo Hart", "character": "", "profile": "file:///etc/passwd"},
    {"name": ""}, "junk",
]}
PERSON = {"id": 6384, "name": "Ana Bright", "profile": "https://img.example/a.jpg",
          "biography": "Bio.", "credits": [
              {"type": "movie", "tmdb_id": 603, "title": "Demo", "year": "1999"},
              {"type": "tv", "tmdb_id": 1399, "title": "Some Show", "year": 2011},
              {"type": "person", "title": "ignored"}, {"type": "movie"}]}


class FakeServer:
    """fetch(url) for MetaClient: answers from a dict, counts the calls."""

    def __init__(self, routes=None, status=200):
        self.routes = routes or {}
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "x", {}, io.BytesIO(b""))
        for part, body in self.routes.items():
            if part in url:
                return json.dumps(body).encode()
        raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def client(tmp_path, server, clock=None):
    return MetaClient("https://meta.example", tmp_path / "cache", server, clock or Clock())


def _ch(name, url, mt=MOVIE, year="", tmdb="", pid=1):
    return Channel(name=name, url=url, media_type=mt, year=year, tmdb=tmdb, playlist_id=pid)


# ---- parsing --------------------------------------------------------------

def test_parse_title_sanitizes_untrusted_json():
    cast = meta.parse_title(TITLE)
    assert [m.name for m in cast] == ["Ana Bright", "Leo Hart"]
    assert cast[0].profile == "https://image.tmdb.org/t/p/w185/abc.jpg"
    assert cast[0].id == "6384"
    assert cast[0].character == "Neo"
    assert cast[1].profile == ""
    assert cast[1].id == ""  # file: URL and bad id dropped
    with pytest.raises(MetaNotFound):
        meta.parse_title({"cast": "nope"})


def test_parse_person_keeps_movies_and_series_only():
    person = meta.parse_person(PERSON)
    assert person.name == "Ana Bright"
    assert person.biography == "Bio."
    assert [(c.type, c.tmdb_id, c.year) for c in person.credits] == [
        (MOVIE, "603", "1999"), (SERIES, "1399", "2011")]


def test_split_cast_text():
    names = [m.name for m in meta.split_cast("Ana Bright, Leo Hart;Ana Bright / X")]
    assert names == ["Ana Bright", "Leo Hart"]
    assert meta.split_cast("") == []


# ---- client: requests, cache and fallbacks ----------------------------------

def test_title_by_tmdb_id_then_cached_on_disk(tmp_path):
    server = FakeServer({"/meta/title": TITLE})
    c = client(tmp_path, server)
    assert c.title(MOVIE, tmdb_id="603", query="ignored", lang="pt-BR")[0].name == "Ana Bright"
    assert "tmdb_id=603" in server.calls[0]
    assert "q=" not in server.calls[0]
    assert "lang=pt-BR" in server.calls[0]
    assert "type=movie" in server.calls[0]
    # A new client (next app start) reads the disk cache: no request.
    c2 = client(tmp_path, server)
    assert c2.title(MOVIE, tmdb_id="603", lang="pt-BR")[0].name == "Ana Bright"
    assert len(server.calls) == 1


def test_title_by_name_and_year_without_tmdb_id(tmp_path):
    server = FakeServer({"/meta/title": TITLE})
    client(tmp_path, server).title(SERIES, query="Duna: Parte Dois", year="2024")
    url = server.calls[0]
    assert "type=series" in url
    assert "q=Duna%3A+Parte+Dois" in url
    assert "year=2024" in url
    with pytest.raises(MetaNotFound):
        client(tmp_path, server).title(MOVIE)  # nothing to ask for


def test_cache_expires_after_ttl(tmp_path):
    server, clock = FakeServer({"/meta/person/6384": PERSON}), Clock()
    c = client(tmp_path, server, clock)
    c.person("6384")
    clock.now += meta.PERSON_TTL - 10
    c.person("6384")
    assert len(server.calls) == 1
    clock.now += 20
    c.person("6384")
    assert len(server.calls) == 2


def test_not_found_is_cached_for_a_day(tmp_path):
    server, clock = FakeServer(), Clock()
    c = client(tmp_path, server, clock)
    for _ in range(2):
        with pytest.raises(MetaNotFound):
            c.person("42")
    assert len(server.calls) == 1
    clock.now += meta.MISS_TTL + 1
    with pytest.raises(MetaNotFound):
        c.person("42")
    assert len(server.calls) == 2


@pytest.mark.parametrize("status", [503, 429, 500])
def test_unavailable_is_not_cached_and_backs_off(tmp_path, status):
    server, clock = FakeServer({"/meta/title": TITLE}, status=status), Clock()
    c = client(tmp_path, server, clock)
    with pytest.raises(MetaUnavailable):
        c.title(MOVIE, tmdb_id="603")
    # Offline for a while: no request at all.
    with pytest.raises(MetaUnavailable):
        c.title(MOVIE, tmdb_id="604")
    assert len(server.calls) == 1
    server.status = 200
    clock.now += meta.OFFLINE_SECS + 1
    assert c.title(MOVIE, tmdb_id="603")  # back online, and nothing bad was cached


def test_network_error_and_bad_json_mean_unavailable(tmp_path):
    def down(_url):
        raise urllib.error.URLError("no route")

    with pytest.raises(MetaUnavailable):
        client(tmp_path, down).person("1")
    with pytest.raises(MetaUnavailable):
        client(tmp_path / "b", lambda _u: b"<html>").person("1")


def test_person_id_must_be_numeric(tmp_path):
    server = FakeServer()
    with pytest.raises(MetaNotFound):
        client(tmp_path, server).person("../../etc")
    assert server.calls == []


# ---- matching to the user's catalog -------------------------------------------

def test_title_key_ignores_accents_years_and_tags():
    assert meta.title_key("Duna: Parte Dois - 2024 [4K]") == "duna parte dois"
    assert meta.title_key("DUNA – PARTE DOIS (2024)") == "duna parte dois"
    assert meta.title_key("Crème Brûlée FHD") == "creme brulee"
    assert meta.title_key("2012 (2009)") == meta.title_key("2012") == "2012"


def test_match_by_tmdb_id_first():
    index = CatalogIndex([_ch("Renamed By Provider", "movie/1.mp4", tmdb="603"),
                          _ch("Demo", "movie/2.mp4", year="1999")])
    found = index.find(Credit(MOVIE, "Demo", year="1999", tmdb_id="603"))
    assert found.url == "movie/1.mp4"


def test_match_by_title_and_year_within_one():
    index = CatalogIndex([_ch("Ação Total (2010)", "movie/1.mp4"),
                          _ch("Old Film", "movie/2.mp4", year="1990"),
                          _ch("Some Show", "series/3", SERIES, year="2011")])
    assert index.find(Credit(MOVIE, "Acao Total", year="2011")) is not None
    assert index.find(Credit(MOVIE, "Old Film", year="1992")) is None  # 2 years off
    assert index.find(Credit(MOVIE, "Some Show", year="2011")) is None  # wrong type
    assert index.find(Credit(SERIES, "x", original_title="Some Show", year="2012"))


def test_same_title_with_another_tmdb_id_is_another_work():
    index = CatalogIndex([_ch("Solaris", "movie/1.mp4", year="2002", tmdb="2103")])
    assert index.find(Credit(MOVIE, "Solaris", year="2002", tmdb_id="593")) is None


def test_match_splits_dedupes_and_sorts_newest_first():
    index = CatalogIndex([
        _ch("Demo", "movie/1.mp4", year="1999", tmdb="603"),
        _ch("Demo", "movie/9.mp4", year="1999", tmdb="603", pid=2),  # other playlist
        _ch("Newer", "movie/2.mp4", year="2020"),
        _ch("Some Show", "series/3", SERIES, year="2011"),
    ])
    credits = [Credit(MOVIE, "Demo", year="1999", tmdb_id="603"),
               Credit(MOVIE, "Newer", year="2020"), Credit(SERIES, "Some Show", year="2011"),
               Credit(MOVIE, "Not In Catalog", year="2001")]
    found_movies, found_series = index.match(credits)
    assert [c.url for c in found_movies] == ["movie/2.mp4", "movie/1.mp4"]
    assert [c.url for c in found_series] == ["series/3"]


def test_fallback_finds_titles_whose_loaded_cast_has_the_name():
    a, b = _ch("A", "movie/1.mp4"), _ch("B", "series/2", SERIES)
    lookup = {(1, "movie/1.mp4"): a, (1, "series/2"): b}.get
    texts = {(1, "movie/1.mp4"): "Ana Bright, Leo Hart", (1, "series/2"): "ANA BRIGHT",
             (1, "gone"): "Ana Bright", (1, "movie/x"): "Someone Else"}
    found_movies, found_series = meta.titles_with_cast("Ána Bright", texts, lookup)
    assert found_movies == [a]
    assert found_series == [b]
    assert meta.titles_with_cast("  ", texts, lookup) == ([], [])


# ---- provider data and storage -------------------------------------------------

def test_tmdb_ids_from_xtream():
    items = [{"stream_id": 1, "name": "A", "tmdb": "603"},
             {"stream_id": 2, "name": "B", "tmdb_id": 0},
             {"stream_id": 3, "name": "C", "tmdb": "abc"}]
    assert [c.tmdb for c in movies(items, {})] == ["603", "", ""]
    assert parse_vod_info({"info": {"tmdb_id": "603"}})["tmdb_id"] == "603"
    assert parse_series_meta({"info": {"tmdb": 1399, "cast": ["A", "B"]}}) == {
        "cast": "A, B", "tmdb_id": "1399"}
    assert parse_series_meta({}) == {}


def test_storage_keeps_tmdb_and_migrates_old_databases(tmp_path):
    path = tmp_path / "t.db"
    db = Storage(path)
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("A", "movie/1.mp4", tmdb="603")])
    assert db.channels(MOVIE)[0].tmdb == "603"
    db.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE channel DROP COLUMN tmdb")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    db = Storage(path)  # v4 -> v5 adds the column back
    assert db.channels(MOVIE)[0].tmdb == ""


def test_cast_service_matches_person_to_the_catalog(tmp_path):
    from ashytv.ui.cast import CastService, search_title

    assert search_title("[L] Duna: Parte Dois - 2024 [4K]") == "Duna: Parte Dois"
    assert search_title("Filme (2019)") == "Filme"
    assert search_title("2012") == "2012"
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [_ch("Demo", "movie/1.mp4", year="1999", tmdb="603"),
                              _ch("Some Show", "series/2", SERIES, year="2011"),
                              _ch("Adult Thing", "movie/3.mp4")])
    server = FakeServer({"/meta/person/6384": PERSON})
    service = CastService(db, client(tmp_path, server))
    results = []
    service.load_person(meta.CastMember("Ana Bright", id="6384"),
                        lambda *args: results.append(args))
    ctx = GLib.MainContext.default()
    for _ in range(200):
        if results:
            break
        ctx.iteration(False)
        GLib.usleep(10_000)
    person, found_movies, found_series, from_tmdb = results[0]
    assert from_tmdb
    assert person.biography == "Bio."
    assert [c.url for c in found_movies] == ["movie/1.mp4"]
    assert [c.url for c in found_series] == ["series/2"]


# ---- marquee ---------------------------------------------------------------------

def test_marquee_offset_pauses_scrolls_and_returns():
    assert marquee_offset(0.5, 80, speed=40, pause=1) == 0
    assert marquee_offset(2.0, 80, speed=40, pause=1) == pytest.approx(40)
    assert marquee_offset(3.5, 80, speed=40, pause=1) == 80
    assert marquee_offset(5.0, 80, speed=40, pause=1) == pytest.approx(40)
    assert marquee_offset(6.5, 80, speed=40, pause=1) == 0  # next cycle
    assert marquee_offset(3.0, 0) == 0
