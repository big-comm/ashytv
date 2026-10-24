"""M3U parser: names with commas, classification, options and series."""

from ashytv.models import MediaType
from ashytv.services.m3u_parser import (
    group_series,
    header_epg_url,
    parse_m3u,
    split_episode,
)
from ashytv.services.text import episode_title, normalize


def _one(extinf: str, url: str = "http://h/stream"):
    return parse_m3u(f"#EXTM3U\n{extinf}\n{url}\n")[0]


def test_name_with_comma_is_kept_whole():
    ch = _one('#EXTINF:-1 tvg-name="X" group-title="Movies",Swift, Daring 7 (2015)')
    assert ch.name == "Swift, Daring 7 (2015)"


def test_comma_inside_attribute_does_not_split_name():
    ch = _one('#EXTINF:-1 tvg-logo="http://a/b,c.png" group-title="A, B",Channel X')
    assert ch.name == "Channel X"
    assert ch.logo == "http://a/b,c.png"
    assert ch.group == "A, B"


def test_url_path_wins_over_group_title():
    # "Live" group named after movies/series (common in Portuguese-language playlists).
    live = _one('#EXTINF:-1 group-title="CANAIS | FILMES E SERIES",Cine 24h',
                "http://h/u/p/123.ts")
    assert live.media_type is MediaType.LIVE
    movie = _one('#EXTINF:-1 group-title="Any",Movie', "http://h/movie/u/p/9.mp4")
    assert movie.media_type is MediaType.MOVIE
    series = _one('#EXTINF:-1 group-title="Any",Ep', "http://h/series/u/p/9.mkv")
    assert series.media_type is MediaType.SERIES


def test_group_prefix_classification_without_url_hint():
    assert _one('#EXTINF:-1 group-title="SERIES - DRAMA",X').media_type is MediaType.SERIES
    assert _one('#EXTINF:-1 group-title="FILMES - AÇÃO",X').media_type is MediaType.MOVIE
    assert _one('#EXTINF:-1 group-title="24 HORAS SERIES",X').media_type is MediaType.LIVE
    assert _one('#EXTINF:-1 group-title="CANAIS CINEMA",X').media_type is MediaType.LIVE


def test_extvlcopt_user_agent_and_referrer():
    text = ("#EXTM3U\n#EXTINF:-1,Channel\n#EXTVLCOPT:http-user-agent=Foo/1.0\n"
            "#EXTVLCOPT:http-referrer=http://ref/\nhttp://h/live.m3u8\n")
    ch = parse_m3u(text)[0]
    assert ch.user_agent == "Foo/1.0"
    assert ch.referrer == "http://ref/"


def test_header_epg_url():
    assert header_epg_url('#EXTM3U url-tvg="http://e/guide.xml.gz"\n') == \
        "http://e/guide.xml.gz"
    assert header_epg_url('#EXTM3U x-tvg-url="http://a.xml,http://b.xml"\n') == \
        "http://a.xml"
    assert header_epg_url("#EXTM3U\n#EXTINF:-1,X\nhttp://h\n") == ""


def test_split_episode():
    assert split_episode("Paper Harbor S02E05") == ("Paper Harbor", 2, 5)
    assert split_episode("Old Town 2001 S01E117") == ("Old Town 2001", 1, 117)
    assert split_episode("Movie without episode") is None


def test_group_series_builds_one_item_per_show():
    text = "#EXTM3U\n" + "".join(
        f'#EXTINF:-1 group-title="SERIES - X",Show S01E0{n}\nhttp://h/series/u/p/{n}.mp4\n'
        for n in (2, 1, 3))
    items, episodes = group_series(parse_m3u(text))
    assert len(items) == 1
    show = items[0]
    assert show.name == "Show" and show.media_type is MediaType.SERIES
    assert [e.episode for e in episodes[show.url]] == [1, 2, 3]


def test_episode_title_cleanup():
    assert episode_title("Starlight Bay 2026 - S01E0001", "Starlight Bay 2026 (2026)", 1) == \
        "Episode 1"
    assert episode_title("Harbor S02E05 - The Bell", "Harbor", 5) == "The Bell"
    assert episode_title("Pilot", "Harbor", 1) == "Pilot"


def test_normalize_removes_accents():
    assert normalize("Séries Ação") == "series acao"
