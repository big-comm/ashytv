"""Logo loader: sized TMDB URLs and the thumbnail cache."""

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from ashytv.services import logos  # noqa: E402


def test_tmdb_images_are_fetched_at_the_shown_size():
    url = "https://image.tmdb.org/t/p/w780/abc.jpg"
    assert logos.sized_url(url, 210) == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert logos.sized_url(url, 96) == "https://image.tmdb.org/t/p/w154/abc.jpg"
    assert logos.sized_url("https://image.tmdb.org/t/p/original/x.png", 780) \
        == "https://image.tmdb.org/t/p/w780/x.png"
    assert logos.sized_url(url, 4000) == "https://image.tmdb.org/t/p/original/abc.jpg"
    other = "https://example.org/poster.jpg"
    assert logos.sized_url(other, 210) == other


def _loader(monkeypatch, tmp_path):
    monkeypatch.setattr(logos.GLib, "idle_add", lambda fn, *a: fn(*a))
    monkeypatch.setattr(logos.http, "cache_dir", lambda sub: tmp_path / sub)
    ld = logos.LogoLoader()
    monkeypatch.setattr(ld, "_run", lambda: None)  # no threads: work runs inline
    return ld


def _load(loader, url):
    got = []
    loader.load(url, got.append, 210)
    loader._work(*loader._next())
    return got[0]


def test_second_load_comes_from_the_thumbnail(tmp_path, monkeypatch):
    url = "https://example.org/poster.jpg"
    first = _loader(monkeypatch, tmp_path)
    first._dir.mkdir(parents=True)
    big = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 1000, 1500)
    big.fill(0x336699FF)
    big.savev(str(first.path_for(url)), "jpeg", [], [])

    tex = _load(first, url)
    assert (tex.get_width(), tex.get_height()) == (140, 210)
    assert first.thumb_for(url, 210).exists()

    first.path_for(url).unlink()  # the original is no longer needed
    again = _load(_loader(monkeypatch, tmp_path), url)
    assert (again.get_width(), again.get_height()) == (140, 210)


def test_originals_downloaded_by_older_versions_are_reused(tmp_path, monkeypatch):
    url = "https://image.tmdb.org/t/p/w780/abc.jpg"
    loader = _loader(monkeypatch, tmp_path)
    loader._dir.mkdir(parents=True)
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 400, 600)
    pb.fill(0x112233FF)
    pb.savev(str(loader.path_for(url)), "jpeg", [], [])

    def no_network(_url, timeout=0):
        raise AssertionError("should not download again")

    monkeypatch.setattr(logos.http, "fetch_bytes_keepalive", no_network)
    tex = _load(loader, url)
    assert tex.get_height() == 210
