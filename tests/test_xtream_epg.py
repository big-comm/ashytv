"""Tests for the Xtream client and the EPG (XMLTV). No network; fictitious credentials."""

import gzip
import io
import json
import time

import pytest

from ashytv.models import MediaType
from ashytv.services import xtream_client as xc
from ashytv.services.epg import EpgGuide, _parse_ts
from ashytv.services.xtream_client import XtreamProvider, parse_provider

WELCOME = """
✅ Usuário: 111222333
✅ Senha: 444555666
▶️ m3u : http://iptv.exemplo.test/get.php?username=111222333&password=444555666&type=m3u_plus&output=mpegts
🟠 DNS: http://iptv.exemplo.test
💳 Assinar: https://loja.exemplo.test/#/checkout/abc
"""


def test_parse_provider_from_getphp():
    prov = parse_provider(WELCOME)
    assert prov is not None
    assert prov.host == "http://iptv.exemplo.test"
    assert prov.username == "111222333"
    assert prov.password == "444555666"
    assert "xmltv.php" in prov.xmltv_url()
    assert "type=m3u_plus" in prov.m3u_url()


def test_provider_json_roundtrip_and_source_key():
    prov = XtreamProvider("http://x.y", "u", "p")
    assert XtreamProvider.from_json(prov.to_json()) == prov
    # Without a password (stored in the keyring) the source key is the same.
    no_pwd = prov.to_json(include_password=False)
    assert "password" not in json.loads(no_pwd)
    assert XtreamProvider.source_key(no_pwd) == XtreamProvider.source_key(prov.to_json())
    assert XtreamProvider.from_json(no_pwd, password="p") == prov


def test_parse_provider_fallback_without_url():
    txt = "DNS: http://meuserver.tv Usuário: abc Senha: xyz"
    prov = parse_provider(txt)
    assert prov is not None
    assert prov.host == "http://meuserver.tv"
    assert prov.username == "abc"
    assert prov.password == "xyz"


def test_credentials_are_url_encoded():
    prov = XtreamProvider("http://h", "us er", "a&b#c")
    url = prov.m3u_url()
    assert "password=a%26b%23c" in url
    assert "username=us+er" in url
    assert prov.stream_url("live/560.ts") == "http://h/live/us%20er/a%26b%23c/560.ts"
    assert prov.api_url("get_series_info", series_id="7").endswith(
        "action=get_series_info&series_id=7")


def test_normalize_host():
    assert xc.normalize_host("servidor.tv:8080/") == "http://servidor.tv:8080"
    assert xc.normalize_host("https://a.b") == "https://a.b"


def test_catalog_converters():
    cats = {"1": "SPORTS", "2": "FILMES - AÇÃO", "3": "SERIES - DRAMA"}
    live = xc.live_channels([{"stream_id": 10, "name": "Channel", "category_id": "1",
                              "epg_channel_id": "C.example", "stream_icon": "http://i"}], cats)
    assert live[0].url == "live/10.ts" and live[0].group == "SPORTS"
    assert live[0].tvg_id == "C.example" and live[0].media_type is MediaType.LIVE

    movies = xc.movies([{"stream_id": 5, "name": "Movie - 2022", "category_id": "2",
                         "rating": "7.5", "container_extension": "mkv", "year": None}], cats)
    assert movies[0].url == "movie/5.mkv"
    assert movies[0].rating == 7.5 and movies[0].year == "2022"

    series = xc.series([{"series_id": 9, "name": "Series", "category_id": "3",
                         "cover": "http://c", "rating": "8", "year": "2020",
                         "backdrop_path": ["http://b1", "http://b2"]}], cats)
    assert series[0].url == "series/9" and series[0].ext_id == "9"
    assert series[0].backdrop == "http://b1" and series[0].year == "2020"


def test_parse_series_info():
    data = {"info": {"name": "Show (2020)"}, "episodes": {
        "2": [{"id": "22", "episode_num": "1", "title": "Show - S02E01",
               "container_extension": "mp4", "info": {"duration_secs": 60}}],
        "1": [{"id": "12", "episode_num": "2", "title": "The Return",
               "container_extension": "mkv", "info": []},
              {"id": "11", "episode_num": "1", "title": "Show - S01E01",
               "container_extension": "mkv", "info": {}}],
    }}
    eps = xc.parse_series_info(data)
    assert [(e.season, e.episode) for e in eps] == [(1, 1), (1, 2), (2, 1)]
    assert eps[0].url == "series/11.mkv" and eps[0].title == "Episode 1"
    assert eps[1].title == "The Return"
    assert eps[2].duration == 60


def test_account_info_rejects_invalid(monkeypatch):
    monkeypatch.setattr(xc.http, "fetch_json", lambda *_a, **_k: {"user_info": {"auth": 0}})
    with pytest.raises(xc.XtreamError):
        xc.account_info(XtreamProvider("http://h", "u", "p"))


def test_load_catalog_with_fake_api(monkeypatch):
    responses = {
        "": {"user_info": {"auth": 1, "status": "Active", "exp_date": "1792889302",
                           "max_connections": "1", "is_trial": "1"}},
        "get_live_categories": [{"category_id": "1", "category_name": "FREE-TO-AIR"}],
        "get_vod_categories": [],
        "get_series_categories": [],
        "get_live_streams": [{"stream_id": 1, "name": "A", "category_id": "1"}],
        "get_vod_streams": [{"stream_id": 2, "name": "B - 2020"}],
        "get_series": [{"series_id": 3, "name": "C"}],
    }

    def fake(url, timeout=0):
        action = url.split("action=")[1].split("&")[0] if "action=" in url else ""
        return responses[action]

    monkeypatch.setattr(xc.http, "fetch_json", fake)
    items, meta = xc.load_catalog(XtreamProvider("http://h", "u", "p"))
    assert [c.media_type for c in items] == [MediaType.LIVE, MediaType.MOVIE,
                                             MediaType.SERIES]
    assert items[0].group == "FREE-TO-AIR"
    assert meta["exp_date"] == 1792889302 and meta["is_trial"] is True


# ---- EPG -----------------------------------------------------------------

def _xml(*progs):
    body = "".join(
        f'<programme start_timestamp="{s}" stop_timestamp="{e}" channel="{c}">'
        f"<title>{t}</title></programme>" for c, s, e, t in progs)
    return f'<?xml version="1.0"?><tv>{body}</tv>'.encode()


def test_epg_now_next():
    now = int(time.time())
    g = EpgGuide()
    g.load_xmltv(_xml(("Aurelia.demo", now - 600, now + 600, "Now"),
                      ("Aurelia.demo", now + 600, now + 1200, "Later")))
    cur, nxt = g.now_next("Aurelia.demo", now)
    assert cur is not None and cur.title == "Now"
    assert nxt is not None and nxt.title == "Later"
    assert g.now_next("Missing.br", now) == (None, None)


def test_epg_gap_has_next_but_no_current():
    g = EpgGuide()
    g.load_xmltv(_xml(("C", 100, 200, "A"), ("C", 300, 400, "B")))
    cur, nxt = g.now_next("C", 250)
    assert cur is None and nxt.title == "B"


def test_epg_dedupes_overlapping_programmes():
    g = EpgGuide()
    g.load_xmltv(_xml(("C", 100, 200, "A"), ("C", 150, 260, "A2"), ("C", 200, 300, "B")))
    assert [p.title for p in g.between("C", 0, 1000)] == ["A", "B"]


def test_epg_between_window():
    g = EpgGuide()
    g.load_xmltv(_xml(("C", 0, 100, "1"), ("C", 100, 200, "2"), ("C", 200, 300, "3")))
    assert [p.title for p in g.between("C", 150, 250)] == ["2", "3"]


def test_epg_gzip_and_file_stream():
    data = _xml(("C", 1, 2, "X"))
    g = EpgGuide()
    g.load_xmltv(io.BytesIO(gzip.compress(data)))
    assert g.programme_count == 1


def test_xmltv_time_respects_timezone():
    # 12:00 UTC = 09:00 at -0300; the epoch is the same regardless of the local timezone.
    assert _parse_ts(None, "20261023120000 +0000") == 1792756800
    assert _parse_ts(None, "20261023090000 -0300") == 1792756800
    assert _parse_ts("1792756800", "junk") == 1792756800
    assert _parse_ts(None, "invalid") == 0


def test_provider_json_is_valid_json():
    assert json.loads(XtreamProvider("http://h", "u", "p").to_json())["host"] == "http://h"
