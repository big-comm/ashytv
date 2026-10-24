"""Smoke tests for the parser and storage (no GUI)."""

from pathlib import Path

from ashytv.models import MediaType
from ashytv.services.m3u_parser import group_series, parse_m3u
from ashytv.services.storage import Storage

SAMPLE = Path(__file__).parent / "sample.m3u"


def test_parse_classifies_media_types():
    channels = parse_m3u(SAMPLE.read_text())
    assert len(channels) == 5
    by_type = {}
    for c in channels:
        by_type.setdefault(c.media_type, 0)
        by_type[c.media_type] += 1
    assert by_type[MediaType.LIVE] == 2
    assert by_type[MediaType.MOVIE] == 2
    assert by_type[MediaType.SERIES] == 1


def test_parse_extracts_attrs():
    channels = parse_m3u(SAMPLE.read_text())
    lumen = next(c for c in channels if c.name == "Lumen One HD")
    assert lumen.tvg_id == "LumenOne.demo"
    assert lumen.group == "Free-to-air"
    assert lumen.logo.endswith("lumen.png")
    assert lumen.url.endswith("lumen.m3u8")


def test_storage_roundtrip(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("Test", str(SAMPLE), "m3u")
    items, episodes = group_series(parse_m3u(SAMPLE.read_text()))
    db.replace_channels(pid, items, episodes)
    assert db.count_channels(pid) == 5
    assert len(db.channels(media_type=MediaType.MOVIE)) == 2
    assert db.has_playlists()
    series = db.channels(MediaType.SERIES)[0]
    assert series.name == "Paper Harbor"
    assert [e.episode for e in db.episodes(series.id)] == [1]
