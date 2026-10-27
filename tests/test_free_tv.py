"""Free TV: the Free-TV filter, the combined playlist and the curated catalog."""

from ashytv.models import MediaType
from ashytv.services import free_tv
from ashytv.services.m3u_parser import group_series, parse_m3u

REMOTE = """#EXTM3U x-tvg-url="https://example.com/guide.xml"
#EXTINF:-1 group-title="Brazil",Chamber TV
https://stream3.camara.gov.br/tv1/manifest.m3u8
#EXTINF:-1 group-title="Brazil",Public Channel
https://tv.example.org/live/playlist.m3u8
#EXTINF:-1 group-title="USA",Platform Channel
https://service-stitcher.clusters.pluto.tv/stitch/hls/channel/1/master.m3u8
#EXTINF:-1 group-title="Brazil",FAST distributor feed
https://rnw-rn.otteravision.com/rnw/rn/rnw_rn.m3u8
#EXTINF:-1 group-title="Brazil",Bare IP
http://203.0.113.7/live/index.m3u8
#EXTINF:-1 group-title="Brazil",Video Site Ⓨ
https://www.youtube.com/watch?v=abc
#EXTINF:-1 group-title="Italy",Signed
https://cdn.example.org/live.m3u8?wmsAuthSign=xyz
#EXTINF:-1 group-title="Czech Republic",Relay
https://relay.example.org/stream.php?ch=1
#EXTINF:-1 group-title="VOD Italy",On demand
https://cdn.example.org/movie.m3u8
"""


def test_filter_keeps_only_broadcasters_streams():
    kept = free_tv.filter_playlist(REMOTE)
    assert kept[1::2] == ["https://stream3.camara.gov.br/tv1/manifest.m3u8",
                          "https://tv.example.org/live/playlist.m3u8"]


def test_sources_roundtrip():
    src = free_tv.make_source("brazil", False)
    assert free_tv.is_free_tv(src) and not free_tv.is_free_tv("https://x/l.m3u")
    assert free_tv.parse_source(src) == ("brazil", False)
    assert free_tv.channels_url("brazil").endswith("/playlists/playlist_brazil.m3u8")
    assert free_tv.channels_url(free_tv.ALL).endswith("/playlist.m3u8")


def test_combined_playlist_has_catalog_and_no_duplicates():
    asked = []

    def read(url):
        asked.append(url)
        return REMOTE

    text = free_tv.build_playlist(free_tv.make_source("brazil", True), read)
    assert asked == [free_tv.channels_url("brazil")]
    assert "x-tvg-url" not in text  # the remote header is not carried over
    urls = [line for line in text.splitlines() if line.startswith("http")]
    assert len(urls) == len(set(urls))
    assert "https://tv.example.org/live/playlist.m3u8" in urls
    items, episodes = group_series(parse_m3u(text))
    kinds = {c.media_type for c in items}
    assert kinds == {MediaType.LIVE, MediaType.MOVIE, MediaType.SERIES}
    assert all(len(eps) > 1 for eps in episodes.values())

    only_tv = free_tv.build_playlist(free_tv.make_source("brazil", False), read)
    assert only_tv.count("#EXTINF") == 2


def test_catalog_entries_are_https_with_licence_notes():
    text = free_tv.CATALOG.read_text(encoding="utf-8")
    channels = parse_m3u(text)
    assert len(channels) > 30
    assert all(c.url.startswith("https://") for c in channels)
    movies = [c for c in channels if c.media_type is MediaType.MOVIE]
    assert movies and all(c.year and c.plot for c in movies)


def test_country_names_and_default(monkeypatch):
    names = free_tv.country_names()
    assert set(names) == set(free_tv.COUNTRIES) and all(names.values())
    for var in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    assert free_tv.default_country() == "brazil"
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert free_tv.default_country() == "uk"


def test_groups_are_localized_after_classification(monkeypatch):
    monkeypatch.setattr(free_tv, "country_names", lambda: {"brazil": "Brasil", "uk": "Reino Unido"})
    chans = parse_m3u('#EXTM3U\n'
                      '#EXTINF:-1 group-title="Movies – Classics",Film (1940)\nhttps://a/1.mp4\n'
                      '#EXTINF:-1 group-title="Brazil",TV\nhttps://a/2.m3u8\n'
                      '#EXTINF:-1 group-title="UK",TV2\nhttps://a/3.m3u8\n'
                      '#EXTINF:-1 group-title="News (AR)",N\nhttps://a/4.m3u8\n')
    free_tv.localize_groups(chans)
    assert [c.group for c in chans] == ["Classics", "Brasil", "Reino Unido", "News (AR)"]
    assert chans[0].media_type is MediaType.MOVIE
