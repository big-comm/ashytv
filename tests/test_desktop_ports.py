"""Features ported from the Android app: guide links by name, Home rails,
speed test, people search."""

import threading
import urllib.error

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from ashytv.models import Channel, MediaType  # noqa: E402
from ashytv.services import home, meta, speedtest  # noqa: E402
from ashytv.services.epg import EpgGuide  # noqa: E402
from ashytv.services.guide_names import GuideNameIndex, normalize_name  # noqa: E402
from ashytv.services.http import is_web_url  # noqa: E402
from ashytv.services.speedtest import Verdict  # noqa: E402
from ashytv.services.storage import Storage  # noqa: E402

# ---- guide names --------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("Canal Ação FHD", "canal acao"),
    ("ESPN 2 [Alt] (BR)", "espn 2"),
    ("Globo Full HD", "globo"),
    ("Discovery HD H265 60fps", "discovery"),
    ("Cinema 4K UHD HDR backup", "cinema"),
    ("TV Brasil SD", "tv brasil"),
    ("HBO-Family.hevc", "hbo family"),
    ("SporTV³", "sportv3"),
    ("[HD]", ""),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_index_links_only_unique_names():
    idx = GuideNameIndex()
    idx.add("Globo.br", ["Globo", "TV Globo"])
    idx.add("Record.br", ["Record"])
    idx.add("Record.pt", ["Record HD"])         # same normalized name: ambiguous
    idx.add("Globo.br", ["Globo FHD"])           # same id again: still unique
    assert idx.match("GLOBO hd") == "globo.br"
    assert idx.match("Record") is None
    assert idx.match("Unknown") is None
    assert idx.match("[FHD]") is None


def test_index_links_skip_channels_the_guide_knows():
    idx = GuideNameIndex()
    idx.add("Globo.br", ["Globo"])
    idx.add("Band.br", ["Band"])
    links = idx.links([("Globo HD", ""), ("Band", "band.br"), ("Band SD", "wrong.id"),
                       ("Nothing", "")])
    assert links == {("Globo HD", ""): "globo.br", ("Band SD", "wrong.id"): "band.br"}


XMLTV = b"""<?xml version="1.0"?>
<tv>
 <channel id="Globo.br"><display-name>Globo</display-name></channel>
 <channel id="Rec.1"><display-name>Record</display-name></channel>
 <channel id="Rec.2"><display-name>Record HD</display-name></channel>
 <programme channel="Globo.br" start_timestamp="100" stop_timestamp="200">
  <title>Jornal</title></programme>
 <programme channel="Rec.1" start_timestamp="100" stop_timestamp="200">
  <title>X</title></programme>
</tv>"""


def test_guide_keeps_programmes_of_channels_linked_by_name():
    g = EpgGuide()
    g.load_xmltv(XMLTV, channels={"other.id"},
                 names=[("Globo FHD", ""), ("Record", ""), ("Other", "other.id")])
    assert g.links == {("Globo FHD", ""): "globo.br"}
    assert g.has_channel("globo.br")
    assert not g.has_channel("rec.1")   # ambiguous name: no guide, not a wrong one


INTERLEAVED = b"""<tv>
 <channel id="A.1"><display-name>Alpha</display-name></channel>
 <programme channel="A.1" start_timestamp="100" stop_timestamp="200"><title>a</title></programme>
 <channel id="B.1"><display-name>Beta</display-name></channel>
 <programme channel="B.1" start_timestamp="100" stop_timestamp="200"><title>b</title></programme>
 <channel id="B.2"><display-name>Beta HD</display-name></channel>
 <programme channel="B.2" start_timestamp="100" stop_timestamp="200"><title>b2</title></programme>
 <channel id="C.1"><display-name>Gamma</display-name></channel>
 <programme channel="C.1" start_timestamp="100" stop_timestamp="200"><title>c</title></programme>
</tv>"""


def test_guide_links_when_channels_and_programmes_interleave():
    g = EpgGuide()
    g.load_xmltv(INTERLEAVED, channels={"x"},
                 names=[("Alpha", ""), ("Beta", ""), ("Gamma [Alt]", "gone.id")])
    assert g.links == {("Alpha", ""): "a.1", ("Gamma [Alt]", "gone.id"): "c.1"}
    assert g.has_channel("a.1") and g.has_channel("c.1")
    # "Beta" was a candidate until "Beta HD" made it ambiguous: dropped.
    assert not g.has_channel("b.1") and not g.has_channel("b.2")


def test_relink_and_coverage(tmp_path):
    db = Storage(tmp_path / "t.db")
    pid = db.add_playlist("P", "x.m3u")
    db.replace_channels(pid, [
        Channel(name="Globo FHD", url="u1"),
        Channel(name="Band", url="u2", tvg_id="Band.br"),
        Channel(name="Other", url="u3"),
        Channel(name="Movie", url="m1", media_type=MediaType.MOVIE),
    ])
    assert ("Globo FHD", "") in db.live_names()
    assert db.relink_guide({("Globo FHD", ""): "globo.br"}) == 1
    known = {"globo.br", "band.br"}
    assert db.guide_coverage(lambda tvg: tvg in known) == {pid: (2, 3)}
    db.close()


def test_is_web_url():
    assert is_web_url("https://example.org/guide.xml.gz")
    assert not is_web_url("ftp://example.org/x")
    assert not is_web_url("guide.xml")
    assert not is_web_url("https://")


# ---- Home ---------------------------------------------------------------------


def _ch(name, kind=MediaType.MOVIE, logo="p.jpg", backdrop=""):
    return Channel(name=name, url=name, media_type=kind, logo=logo, backdrop=backdrop)


def test_featured_alternates_titles_with_pictures():
    movies = [_ch("m0", logo=""), _ch("m1"), _ch("m2"), _ch("m3"), _ch("m4")]
    series = [_ch("s1", MediaType.SERIES, logo="", backdrop="b.jpg"),
              _ch("s2", MediaType.SERIES), _ch("s3", MediaType.SERIES)]
    names = [c.name for c in home.featured(movies, series)]
    assert names == ["m1", "s1", "m2", "s2", "m3"]
    assert [c.name for c in home.featured(movies[:2], [])] == ["m1"]
    assert home.featured([], []) == []


def test_group_rails_biggest_first_small_groups_out():
    movie_groups = [(f"M{i}", 10 + i) for i in range(8)] + [("Tiny", 3)]
    series_groups = [("S big", 100), ("S four", 4), ("S small", 2), ("S mid", 12)]
    rails = home.group_rails(movie_groups, series_groups)
    assert len(rails) == home.GROUP_RAILS
    assert rails[0] == (MediaType.SERIES, "S big", 100)
    assert all(n >= home.MIN_GROUP_TITLES for _t, _g, n in rails)
    counts = [n for _t, _g, n in rails]
    assert counts == sorted(counts, reverse=True)
    assert "Tiny" not in {g for _t, g, _n in rails}
    few = home.group_rails([("A", 4), ("B", 3)], [])
    assert few == [(MediaType.MOVIE, "A", 4)]


def test_rail_limit_and_see_more():
    items = [_ch(str(i)) for i in range(home.RAIL_TITLES + 1)]
    shown, more = home.rail(items)
    assert len(shown) == home.RAIL_TITLES and more
    shown, more = home.rail(items[:home.RAIL_TITLES])
    assert len(shown) == home.RAIL_TITLES and not more


# ---- speed test ---------------------------------------------------------------


@pytest.mark.parametrize("mbps, expected", [
    (100, Verdict.UHD), (25, Verdict.UHD), (24.9, Verdict.FHD), (10, Verdict.FHD),
    (9.99, Verdict.HD), (5, Verdict.HD), (4, Verdict.SD), (3, Verdict.SD),
    (2.9, Verdict.LOW), (0, Verdict.LOW),
])
def test_verdict(mbps, expected):
    assert speedtest.verdict(mbps) is expected


def test_median_and_slow_server():
    assert speedtest.median([300, 20, 30, 25]) == 30
    assert speedtest.median([5]) == 5
    assert speedtest.median([]) is None
    assert speedtest.server_is_slow(None)
    assert speedtest.server_is_slow(801)
    assert not speedtest.server_is_slow(800)
    assert speedtest.mbps(1_250_000, 1.0) == pytest.approx(10.0)
    assert speedtest.mbps(10, 0) == 0.0


def test_server_root_drops_credentials_and_path():
    assert speedtest.server_root("https://u:p@host.example:8080/get.php?username=a") \
        == "https://host.example:8080/"
    assert speedtest.server_root("https://host.example/list.m3u") == "https://host.example/"
    assert speedtest.server_root("/home/me/list.m3u") == ""


class _Resp:
    def __init__(self, size=0):
        self.left = size

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, n):
        n = min(n, self.left)
        self.left -= n
        return b"x" * n


def test_latency_counts_http_errors_as_answers():
    ticks = iter([0.0, 0.05, 1.0, 1.02, 2.0, 2.03])

    def opener(req, timeout):
        if req.get_method() != "HEAD":
            raise AssertionError
        if next(opener.calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return _Resp()
    opener.calls = iter(range(10))
    ms = speedtest.latency_ms("https://x.example/", tries=3, opener=opener,
                              clock=lambda: next(ticks))
    assert ms == pytest.approx(30)

    def down(req, timeout):
        raise OSError("down")
    assert speedtest.latency_ms("https://x.example/", tries=2, opener=down) is None


def test_download_measures_and_stops():
    meter = speedtest.Meter()
    result = speedtest.download_mbps(meter, url="https://x.example/",
                                     streams=2, seconds=0.2,
                                     opener=lambda req, timeout: _Resp(1 << 20))
    assert result is not None and result > 0
    assert meter.bytes > 0

    def fail(req, timeout):
        raise OSError("no")
    assert speedtest.download_mbps(speedtest.Meter(), streams=1, seconds=0.1,
                                   opener=fail) is None
    cancel = threading.Event()
    cancel.set()
    assert speedtest.download_mbps(speedtest.Meter(), cancel, streams=1, seconds=0.1,
                                   opener=lambda r, timeout: _Resp(10)) is None


# ---- people search ------------------------------------------------------------


def test_people_parse_and_query(tmp_path):
    asked = []

    def fetch(url):
        asked.append(url)
        return (b'{"items": [{"id": 287, "name": "Brad Pitt", "profile": "/a.jpg",'
                b' "known_for": "Acting"}, {"id": 0, "name": "No id"}, "junk",'
                b' {"id": 5, "name": ""}]}')
    client = meta.MetaClient("https://panel.example", tmp_path, fetch=fetch)
    people = client.people("  brad  ", "pt-BR")
    assert [(p.id, p.name, p.character) for p in people] == [("287", "Brad Pitt", "Acting")]
    assert people[0].profile.startswith("https://image.tmdb.org/")
    assert "/api/v1/meta/people?" in asked[0] and "q=brad" in asked[0]
    assert client.people("b") == []          # the panel refuses 1 character
    assert client.people("x" * 81) == []
    assert len(asked) == 1
    client.people("brad", "pt-BR")           # cached
    assert len(asked) == 1
