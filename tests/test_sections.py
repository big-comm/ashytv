"""Switching Live channels, Movies or Series off.

The rules (services/sections.py) and the one place they are applied
(services/storage.py). Same cases as the Samsung/LG and Android apps, so the
four clients cannot drift apart without a test saying so.
"""

from ashytv.models import Channel, MediaType
from ashytv.services import sections
from ashytv.services.storage import Storage

NONE: set[MediaType] = set()
BOTH_VOD = {MediaType.MOVIE, MediaType.SERIES}


def _ch(name, url, mt=MediaType.LIVE, group="G"):
    return Channel(name=name, url=url, media_type=mt, group=group)


def _db(tmp_path) -> Storage:
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("L", "src")
    db.replace_channels(pid, [
        _ch("Ch 1", "live/1"), _ch("Ch 2", "live/2"),
        _ch("M 1", "movie/1", MediaType.MOVIE), _ch("M 2", "movie/2", MediaType.MOVIE),
        _ch("S 1", "series/1", MediaType.SERIES),
    ])
    return db


# ---- the rules -------------------------------------------------------------


def test_the_last_section_on_cannot_be_switched_off():
    assert sections.can_hide(NONE, MediaType.LIVE)
    assert sections.can_hide({MediaType.MOVIE}, MediaType.LIVE)
    assert not sections.can_hide(BOTH_VOD, MediaType.LIVE)
    assert sections.toggle(BOTH_VOD, MediaType.LIVE, hide=True) == BOTH_VOD
    # Switching one back on is always allowed.
    assert sections.can_hide(BOTH_VOD, MediaType.MOVIE)
    assert sections.toggle(BOTH_VOD, MediaType.MOVIE, hide=False) == {MediaType.SERIES}


def test_toggling_twice_returns_to_where_it_started():
    once = sections.toggle(NONE, MediaType.SERIES, hide=True)
    assert once == {MediaType.SERIES}
    assert sections.toggle(once, MediaType.SERIES, hide=False) == NONE
    # Hiding what is already hidden changes nothing.
    assert sections.toggle(once, MediaType.SERIES, hide=True) == once


def test_home_goes_only_when_both_catalogs_go():
    assert not sections.home_hidden(NONE)
    for one in ({MediaType.LIVE}, {MediaType.MOVIE}, {MediaType.SERIES}):
        assert not sections.home_hidden(one)
    assert sections.home_hidden(BOTH_VOD)


def test_the_menu_drops_the_entries_with_nothing_behind_them():
    pages = ["home", "favorites", "channels", "movies", "series",
             "recordings", "lists", "settings"]
    assert sections.visible_pages(pages, NONE) == pages
    # Recordings stays: those files are on the disk already.
    assert sections.visible_pages(pages, BOTH_VOD) == [
        "channels", "recordings", "lists", "settings"]
    assert sections.visible_pages(pages, {MediaType.LIVE}) == [
        "home", "favorites", "movies", "series", "recordings", "lists", "settings"]


def test_home_survives_while_either_catalog_does():
    assert sections.default_page(NONE) == "home"
    assert sections.default_page({MediaType.LIVE}) == "home"
    assert sections.default_page({MediaType.MOVIE}) == "home"
    assert sections.default_page({MediaType.LIVE, MediaType.SERIES}) == "home"
    assert sections.default_page(BOTH_VOD) == "channels"


def test_a_hand_edited_settings_file_cannot_empty_the_app():
    assert sections.parse(None) == NONE
    assert sections.parse([]) == NONE
    assert sections.parse(["movie", "nonsense"]) == {MediaType.MOVIE}
    assert sections.parse(["movie", 7, None]) == {MediaType.MOVIE}
    # All three off is not a state the screens can reach: read as none.
    assert sections.parse(["live", "movie", "series"]) == NONE


def test_the_stored_names_survive_a_round_trip():
    for media_type in sections.SECTIONS:
        assert sections.parse(sections.names({media_type})) == {media_type}
    # A fixed order, so settings.json stops churning on every change.
    assert sections.names(BOTH_VOD) == ["movie", "series"]
    assert sections.names(reversed(sections.SECTIONS)) == ["live", "movie", "series"]


# ---- the one place they are applied ----------------------------------------


def test_a_switched_off_section_empties_its_catalog(tmp_path):
    db = _db(tmp_path)
    assert [len(db.channels(t)) for t in sections.SECTIONS] == [2, 2, 1]

    db.hidden_sections = {MediaType.MOVIE}
    assert [len(db.channels(t)) for t in sections.SECTIONS] == [2, 0, 1]
    assert db.count(MediaType.MOVIE) == 0
    assert db.groups(MediaType.MOVIE) == []
    # The others are untouched.
    assert db.count(MediaType.LIVE) == 2 and db.count(MediaType.SERIES) == 1

    db.hidden_sections = BOTH_VOD
    assert [len(db.channels(t)) for t in sections.SECTIONS] == [2, 0, 0]
    # Asking for everything at once must not bring them back either.
    assert {c.media_type for c in db.channels()} == {MediaType.LIVE}

    db.hidden_sections = set()
    assert [len(db.channels(t)) for t in sections.SECTIONS] == [2, 2, 1]


def test_search_and_favorites_follow_the_same_cut(tmp_path):
    db = _db(tmp_path)
    movie = db.channels(MediaType.MOVIE)[0]
    db.set_favorite(movie, True)
    assert db.channels(MediaType.MOVIE, favorites=True)
    assert db.channels(search="M ")

    db.hidden_sections = {MediaType.MOVIE}
    assert db.channels(MediaType.MOVIE, favorites=True) == []
    assert [c.name for c in db.channels(search="M ")] == []
    # The favorite itself is kept: switching the section back on restores it.
    db.hidden_sections = set()
    assert [c.name for c in db.channels(MediaType.MOVIE, favorites=True)] == ["M 1"]


def test_continue_watching_drops_only_the_hidden_kinds(tmp_path):
    db = _db(tmp_path)
    for media_type in sections.SECTIONS:
        ch = db.channels(media_type)[0]
        db.record_history(ch.playlist_id, ch.url, media_type, ch.name,
                          position=10, duration=100)

    assert len(db.history(20)) == 3
    db.hidden_sections = {MediaType.MOVIE}
    kinds = {r["media_type"] for r in db.history(20)}
    assert kinds == {MediaType.LIVE.value, MediaType.SERIES.value}
    assert db.recent_channels(MediaType.MOVIE) == []
    assert len(db.recent_channels(MediaType.LIVE)) == 1
