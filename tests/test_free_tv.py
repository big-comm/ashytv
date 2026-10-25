from ashytv.models import MediaType
from ashytv.services import free_tv
from ashytv.services.m3u_parser import iter_m3u, parse_m3u

FREE_LIST = """#EXTM3U
#EXTINF:-1 tvg-id="Demo.xx" group-title="Entertainment;Movies;Music",Demo Movies (720p)
https://example.com/demo/playlist.m3u8
#EXTINF:-1 group-title="Series",Demo Series Channel
https://example.com/series/playlist.m3u8
"""


def test_playlist_urls():
    assert free_tv.playlist_url("br") == "https://iptv-org.github.io/iptv/countries/br.m3u"
    assert free_tv.playlist_url(free_tv.ALL_BY_CATEGORY).endswith("/index.category.m3u")
    assert free_tv.is_free_tv(free_tv.playlist_url("us"))
    assert not free_tv.is_free_tv("https://example.com/list.m3u")


def test_country_names_cover_every_playlist():
    names = free_tv.country_names()
    assert set(names) == free_tv.COUNTRIES
    assert all(names.values())


def test_default_country(monkeypatch):
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert free_tv.default_country() == "uk"
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    assert free_tv.default_country() == "br"


def test_first_category_and_live_only():
    chans = parse_m3u(FREE_LIST)
    assert chans[0].group == "Entertainment"
    live = list(iter_m3u(FREE_LIST, live_only=True))
    assert {c.media_type for c in live} == {MediaType.LIVE}
