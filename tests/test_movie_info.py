"""Movie details from the Xtream API, track names and preferred languages."""

from ashytv.player.mpv_widget import track_label
from ashytv.services import languages
from ashytv.services.xtream_client import parse_vod_info

SAMPLE = {"info": {
    "o_name": "Original Title", "plot": "A long synopsis.", "director": "Jane Doe",
    "cast": "A. Actor, B. Actor", "genre": "Comedy", "country": "Demoland",
    "release_date": "1985-08-09", "duration_secs": 5220, "rating": 6.1,
    "backdrop_path": ["https://img.example/b.jpg"], "youtube_trailer": "abc123",
    "tmdb_id": 19357, "subtitles": [{"url": "https://subs.example/pt.srt"}, "bad"],
    "video": {"codec_name": "h264", "width": 1920, "height": 1080},
    "audio": {"codec_name": "aac", "channels": 6, "tags": {"language": "por"}},
}}


def test_parse_vod_info():
    info = parse_vod_info(SAMPLE)
    assert info["plot"] == "A long synopsis." and info["original_title"] == "Original Title"
    assert info["duration"] == 5220 and info["backdrop"] == "https://img.example/b.jpg"
    assert info["trailer"] == "https://www.youtube.com/watch?v=abc123"
    assert info["tmdb_url"] == "https://www.themoviedb.org/movie/19357"
    assert info["video"] == "1080p H264"
    assert info["audio"].startswith("AAC 5.1 — ")
    assert info["subtitles"] == ["https://subs.example/pt.srt"]
    assert parse_vod_info({}) == {} and parse_vod_info({"info": []}) == {}


def test_preferred_languages():
    assert languages.preferred_codes("pt_BR") == ["pt-BR", "pt", "por", "pob"]
    assert languages.preferred_codes("de_DE")[:3] == ["de-DE", "de", "deu"]
    assert languages.preferred_codes("") == []


def test_track_labels():
    assert track_label({"id": 1, "lang": "eng", "codec": "aac", "demux-channel-count": 2}) \
        .endswith("— AAC 2.0")
    assert "SRT" in track_label({"id": 2, "codec": "subrip", "title": "SDH"})
    assert track_label({"id": 3})  # never empty
