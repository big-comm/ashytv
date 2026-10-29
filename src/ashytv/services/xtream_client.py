"""Xtream Codes provider support.

Lets the user paste the provider's welcome message (or host/username/password),
derive the playlist (m3u_plus), EPG (xmltv.php) and stream URLs, and read the
native catalog through `player_api.php` (channels, movies, series and episodes).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode, urlparse

from ..i18n import _
from ..models import Channel, Episode, MediaType
from . import http, languages
from .text import episode_title

# get.php?username=...&password=... — the most reliable way to extract everything.
_GETPHP_RE = re.compile(
    r"(https?://[^\s/]+)/get\.php\?([^\s\"'>]+)", re.IGNORECASE
)
_HOST_RE = re.compile(r"https?://[^\s\"'>`]+", re.IGNORECASE)
_USER_RE = re.compile(r"(?:Usu[aá]rio|username)\s*[:=]?\s*`?([A-Za-z0-9_.-]+)`?", re.I)
# Passwords may carry symbols ("Ab@123!"): everything up to a space or backtick.
_PASS_RE = re.compile(r"(?:Senha|password)\s*[:=]?\s*`?([^\s`]+)`?", re.I)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


@dataclass(slots=True)
class XtreamProvider:
    host: str          # e.g. http://server.example:8080
    username: str
    password: str

    def _base(self, path: str, **params: str) -> str:
        query = urlencode({"username": self.username, "password": self.password,
                           **params})
        return f"{self.host}/{path}?{query}"

    def m3u_url(self) -> str:
        return self._base("get.php", type="m3u_plus", output="ts")

    def xmltv_url(self) -> str:
        return self._base("xmltv.php")

    def api_url(self, action: str = "", **params: str) -> str:
        if action:
            params = {"action": action, **params}
        return self._base("player_api.php", **params)

    def stream_url(self, path: str) -> str:
        """'live/560.ts' -> http://host/live/<user>/<password>/560.ts"""
        kind, _sep, rest = path.partition("/")
        user = quote(self.username, safe="")
        pwd = quote(self.password, safe="")
        return f"{self.host}/{kind}/{user}/{pwd}/{rest}"

    def to_json(self, include_password: bool = True) -> str:
        data = {"host": self.host, "username": self.username}
        if include_password:
            data["password"] = self.password
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, data: str, password: str | None = None) -> XtreamProvider:
        d = json.loads(data)
        pwd = password if password is not None else d.get("password", "")
        return cls(host=d["host"], username=d["username"], password=pwd)

    @staticmethod
    def source_key(data: str) -> str:
        """Source without password — identifies the playlist in the database."""
        d = json.loads(data)
        return json.dumps({"host": d["host"], "username": d["username"]},
                          sort_keys=True)


def normalize_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        host = "http://" + host
    return host


def parse_provider(text: str) -> XtreamProvider | None:
    """Extract host/username/password from text pasted from the provider.

    Strategy: first try a get.php URL (which already carries everything); if
    there is none, combine the first http host with the username/password lines.
    """
    text = text.strip()

    m = _GETPHP_RE.search(text)
    if m:
        host = m.group(1).rstrip("/")
        qs = _query(m.group(2))
        user = qs.get("username", "")
        pwd = qs.get("password", "")
        if host and user and pwd:
            return XtreamProvider(host=host, username=user, password=pwd)

    # Fallback: host (ignoring checkout/app URLs) + loose username/password.
    host = _pick_host(text)
    um = _USER_RE.search(text)
    pm = _PASS_RE.search(text)
    if host and um and pm:
        return XtreamProvider(
            host=host.rstrip("/"), username=um.group(1), password=pm.group(1)
        )
    return None


def _query(qs: str) -> dict[str, str]:
    """Query string to dict without parse_qs's "+" -> space: a password
    pasted as "ab+cd" means "ab+cd"."""
    out: dict[str, str] = {}
    for part in qs.split("&"):
        key, sep, value = part.partition("=")
        if sep and key not in out:
            out[key] = unquote(value)
    return out


# Lines that usually announce the server in a provider message.
_SERVER_LINE_RE = re.compile(r"(dns|servidor|server|host|portal|url)", re.I)


def _pick_host(text: str) -> str | None:
    """Pick the server host from free text.

    Preference: a URL on a line that mentions DNS/server; then the first
    "domain-only" URL (no path), which is how servers are advertised.
    URLs with a path (stores, payment pages, downloads) are ignored.
    """
    candidates: list[tuple[int, str]] = []
    for line in text.splitlines():
        for url in _HOST_RE.findall(line):
            parsed = urlparse(url)
            bare = parsed.path in ("", "/") and not parsed.query
            if not bare:
                continue
            rank = 0 if _SERVER_LINE_RE.search(line) else 1
            candidates.append((rank, f"{parsed.scheme}://{parsed.netloc.lower()}"))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


# ---- player_api.php ----------------------------------------------------

class XtreamError(Exception):
    pass


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _year(item: dict, name: str) -> str:
    year = str(item.get("year") or "").strip()
    if year.isdigit():
        return year
    for key in ("releaseDate", "release_date"):
        value = str(item.get(key) or "")
        if value[:4].isdigit():
            return value[:4]
    m = _YEAR_RE.search(name)
    return m.group(1) if m else ""


def account_info(provider: XtreamProvider) -> dict:
    """user_info + server_info. Raises XtreamError if the account is invalid."""
    data = http.fetch_json(provider.api_url(), timeout=30)
    info = data.get("user_info") if isinstance(data, dict) else None
    if not info or str(info.get("auth")) != "1":
        raise XtreamError(_("Invalid username or password"))
    if str(info.get("status", "Active")).lower() not in ("active", ""):
        raise XtreamError(_("Account status: {status}").format(status=info.get("status")))
    return data


def _categories(provider: XtreamProvider, action: str) -> dict[str, str]:
    data = http.fetch_json(provider.api_url(action), timeout=60) or []
    if not isinstance(data, list):
        return {}
    return {str(c.get("category_id")): str(c.get("category_name") or "")
            for c in data if isinstance(c, dict)}


def live_channels(items: list, cats: dict[str, str]) -> list[Channel]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("stream_id")
        if sid is None:
            continue
        out.append(Channel(
            name=str(it.get("name") or "").strip(),
            url=f"live/{sid}.ts",
            media_type=MediaType.LIVE,
            group=cats.get(str(it.get("category_id")), ""),
            tvg_id=str(it.get("epg_channel_id") or ""),
            logo=str(it.get("stream_icon") or ""),
            ext_id=str(sid),
            added=_int(it.get("added")),
        ))
    return out


def movies(items: list, cats: dict[str, str]) -> list[Channel]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("stream_id")
        if sid is None:
            continue
        name = str(it.get("name") or "").strip()
        ext = str(it.get("container_extension") or "mp4")
        out.append(Channel(
            name=name,
            url=f"movie/{sid}.{ext}",
            media_type=MediaType.MOVIE,
            group=cats.get(str(it.get("category_id")), ""),
            logo=str(it.get("stream_icon") or ""),
            ext_id=str(sid),
            rating=_float(it.get("rating")),
            year=_year(it, name),
            plot=str(it.get("plot") or ""),
            genre=str(it.get("genre") or ""),
            added=_int(it.get("added")),
        ))
    return out


def series(items: list, cats: dict[str, str]) -> list[Channel]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("series_id")
        if sid is None:
            continue
        name = str(it.get("name") or "").strip()
        backdrop = it.get("backdrop_path") or ""
        if isinstance(backdrop, list):
            backdrop = backdrop[0] if backdrop else ""
        out.append(Channel(
            name=name,
            url=f"series/{sid}",
            media_type=MediaType.SERIES,
            group=cats.get(str(it.get("category_id")), ""),
            logo=str(it.get("cover") or ""),
            ext_id=str(sid),
            rating=_float(it.get("rating")),
            year=_year(it, name),
            plot=str(it.get("plot") or ""),
            genre=str(it.get("genre") or ""),
            backdrop=str(backdrop or ""),
            added=_int(it.get("last_modified")),
        ))
    return out


def load_catalog(provider: XtreamProvider) -> tuple[list[Channel], dict]:
    """Download the full catalog (live + movies + series) and account data."""
    info = account_info(provider)
    result: list[Channel] = []
    for cat_action, action, convert in (
        ("get_live_categories", "get_live_streams", live_channels),
        ("get_vod_categories", "get_vod_streams", movies),
        ("get_series_categories", "get_series", series),
    ):
        cats = _categories(provider, cat_action)
        items = http.fetch_json(provider.api_url(action), timeout=180) or []
        if isinstance(items, list):
            result.extend(convert(items, cats))
    user = info.get("user_info", {})
    meta = {
        "status": user.get("status", ""),
        "exp_date": _int(user.get("exp_date")),
        "max_connections": _int(user.get("max_connections")),
        "is_trial": str(user.get("is_trial", "0")) == "1",
    }
    return result, meta


def parse_series_info(data: dict) -> list[Episode]:
    episodes: list[Episode] = []
    seasons = data.get("episodes") if isinstance(data, dict) else None
    info_block = data.get("info") if isinstance(data, dict) else None
    series_name = str((info_block or {}).get("name") or "") if isinstance(info_block, dict) else ""
    if isinstance(seasons, list):  # some servers send a list of lists
        seasons = {str(i + 1): eps for i, eps in enumerate(seasons)}
    if not isinstance(seasons, dict):
        seasons = {}
    for season_key, items in seasons.items():
        if not isinstance(items, list):
            continue
        for it in items or []:
            if not isinstance(it, dict):
                continue
            info = it.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            ext = str(it.get("container_extension") or "mp4")
            episodes.append(Episode(
                season=_int(it.get("season") or season_key) or 1,
                episode=_int(it.get("episode_num")),
                title=episode_title(str(it.get("title") or ""), series_name,
                                    _int(it.get("episode_num"))),
                url=f"series/{it.get('id')}.{ext}",
                logo=str(info.get("movie_image") or info.get("cover_big") or ""),
                plot=str(info.get("plot") or info.get("description") or ""),
                duration=_int(info.get("duration_secs")),
            ))
    episodes.sort(key=lambda e: (e.season, e.episode))
    return episodes


def _media_line(stream) -> str:
    """ffprobe-style stream info -> '1080p H264' / 'AAC 5.1 (por)'."""
    if not isinstance(stream, dict):
        return ""
    codec = str(stream.get("codec_name") or "").upper()
    if stream.get("height"):
        return f"{_int(stream['height'])}p {codec}".strip()
    channels = _int(stream.get("channels"))
    layout = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(channels, "")
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    lang = languages.language_name(str(tags.get("language") or ""))
    return " ".join(p for p in (codec, layout, f"— {lang}" if lang else "") if p)


def _text(value) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    return str(value or "").strip()


def parse_vod_info(data: dict) -> dict:
    """get_vod_info -> the details shown for a movie (missing keys are '')."""
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return {}
    backdrop = info.get("backdrop_path") or ""
    if isinstance(backdrop, list):
        backdrop = backdrop[0] if backdrop else ""
    trailer = _text(info.get("youtube_trailer"))
    if trailer and "://" not in trailer:
        trailer = f"https://www.youtube.com/watch?v={trailer}"
    elif not trailer.startswith(("http://", "https://")):
        trailer = ""  # file://, smb://… from a provider must not reach the launcher
    tmdb = _text(info.get("tmdb_id"))
    subtitles = info.get("subtitles") if isinstance(info.get("subtitles"), list) else []
    return {
        "plot": _text(info.get("plot") or info.get("description")),
        "original_title": _text(info.get("o_name")),
        "director": _text(info.get("director")),
        "cast": _text(info.get("cast") or info.get("actors")),
        "genre": _text(info.get("genre")),
        "country": _text(info.get("country")),
        "released": _text(info.get("release_date") or info.get("releasedate")),
        "duration": _int(info.get("duration_secs")),
        "rating": _float(info.get("rating")),
        "age": _text(info.get("mpaa_rating") or info.get("age")),
        "backdrop": _text(backdrop),
        "trailer": trailer,
        "tmdb_url": f"https://www.themoviedb.org/movie/{tmdb}" if tmdb.isdigit() else "",
        "video": _media_line(info.get("video")),
        "audio": _media_line(info.get("audio")),
        "subtitles": [s for s in (_subtitle_url(x) for x in subtitles) if s],
    }


def _subtitle_url(item) -> str:
    if isinstance(item, str):
        return item if item.startswith(("http://", "https://")) else ""
    if isinstance(item, dict):
        url = str(item.get("url") or item.get("file") or "")
        return url if url.startswith(("http://", "https://")) else ""
    return ""


def vod_info(provider: XtreamProvider, vod_id: str) -> dict:
    data = http.fetch_json(provider.api_url("get_vod_info", vod_id=vod_id), timeout=30)
    return parse_vod_info(data)


def series_episodes(provider: XtreamProvider, series_id: str) -> list[Episode]:
    data = http.fetch_json(provider.api_url("get_series_info", series_id=series_id),
                           timeout=60)
    return parse_series_info(data)
