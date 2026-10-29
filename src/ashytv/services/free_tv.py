"""Free TV: free-to-air channels plus a curated catalog of open content.

Two sources, combined into one playlist:

- the Free-TV list (https://github.com/Free-TV/IPTV, the default list of Linux
  Mint's Hypnotix), fetched by URL and *filtered*: channels from commercial
  streaming platforms (Pluto TV, Samsung TV Plus…), YouTube, bare IP
  addresses, relays and copied access tokens are dropped, so what is left are
  broadcasters' own free streams;
- ``data/free_catalog.m3u``, a list curated by AshyTV with explicit open
  licences or public domain works (see the comments in that file).

AshyTV only links to these streams; it does not host anything.
"""

from __future__ import annotations

import gettext
import json
import locale
import os
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..i18n import _

SCHEME = "freetv:"
ALL = "all"
FREE_TV_URL = "https://raw.githubusercontent.com/Free-TV/IPTV/master/"
FREE_TV_PROJECT = "https://github.com/Free-TV/IPTV"
CATALOG = Path(__file__).resolve().parent.parent / "data" / "free_catalog.m3u"

# Free-TV country playlists (playlists/playlist_<slug>.m3u8) -> ISO 3166 code.
COUNTRIES = {
    "albania": "al", "andorra": "ad", "argentina": "ar", "armenia": "am",
    "australia": "au", "austria": "at", "azerbaijan": "az", "belarus": "by",
    "belgium": "be", "bosnia_and_herzegovina": "ba", "brazil": "br", "bulgaria": "bg",
    "canada": "ca", "chad": "td", "chile": "cl", "china": "cn", "costa_rica": "cr",
    "croatia": "hr", "cyprus": "cy", "czech_republic": "cz", "denmark": "dk",
    "dominican_republic": "do", "egypt": "eg", "estonia": "ee", "faroe_islands": "fo",
    "finland": "fi", "france": "fr", "georgia": "ge", "germany": "de", "greece": "gr",
    "greenland": "gl", "hong_kong": "hk", "hungary": "hu", "iceland": "is",
    "india": "in", "indonesia": "id", "iran": "ir", "iraq": "iq", "ireland": "ie",
    "israel": "il", "italy": "it", "japan": "jp", "kazakhstan": "kz", "kenya": "ke",
    "korea": "kr", "kosovo": "xk", "latvia": "lv", "lebanon": "lb", "lithuania": "lt",
    "luxembourg": "lu", "macau": "mo", "malta": "mt", "mexico": "mx", "moldova": "md",
    "monaco": "mc", "mongolia": "mn", "montenegro": "me", "netherlands": "nl",
    "nigeria": "ng", "north_korea": "kp", "north_macedonia": "mk", "norway": "no",
    "paraguay": "py", "peru": "pe", "poland": "pl", "portugal": "pt", "qatar": "qa",
    "romania": "ro", "russia": "ru", "san_marino": "sm", "saudi_arabia": "sa",
    "serbia": "rs", "slovakia": "sk", "slovenia": "si", "somalia": "so", "spain": "es",
    "sweden": "se", "switzerland": "ch", "taiwan": "tw", "trinidad": "tt",
    "turkey": "tr", "turkmenistan": "tm", "uk": "gb", "ukraine": "ua",
    "united_arab_emirates": "ae", "usa": "us", "venezuela": "ve",
}

# ---- filter -----------------------------------------------------------------

# Commercial streaming platforms and the proxies that re-serve them, plus
# video sites whose terms forbid playback outside their own player.
_BLOCKED_HOSTS = (
    "pluto.tv", "jmp2.uk", "mjh.nz", "samsung", "wurl.tv", "wurl.com", "amagi.tv",
    "roku", "plex.tv", "tubi", "xumo", "rakuten", "redbox", "stirr", "frndly",
    "localnow", "ottera", "youtube.com", "youtu.be", "dailymotion.com", "twitch.tv",
    # relays/forwarders of other broadcasters' streams
    "mxnticek.eu", "mcquack.net", "tvkaista.net", "7m.pl", "mov3.co",
    "streams.ovh", "bozztv.com", "lexanetwork.com",
)
_RELAY_WORDS = ("relay", "forward", "proxy", "restream", ".php")
# Signed URLs: a token copied from someone's session, not a public stream.
_TOKEN_RE = re.compile(r"[?&](?:wmsAuthSign|token|hdnts|hdnea|auth|signature|"
                       r"expires|md5|e)=", re.IGNORECASE)
# IPv4, or IPv6 (urlsplit().hostname drops the brackets: any ':' means one).
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$|:")


def allowed(group: str, url: str) -> bool:
    """True for a broadcaster's own free stream (see the module docstring)."""
    if group.casefold().startswith("vod") or not url.startswith(("http://", "https://")):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or _IP_RE.search(host) or any(b in host for b in _BLOCKED_HOSTS):
        return False
    rest = (parts.path + "?" + parts.query).lower()
    return not (any(w in rest for w in _RELAY_WORDS) or _TOKEN_RE.search(url))


def filter_playlist(text: str, keep_all: bool = False) -> list[str]:
    """M3U lines (#EXTINF + URL pairs) of the allowed channels."""
    out: list[str] = []
    extinf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            extinf = line
        elif line and not line.startswith("#") and extinf:
            m = re.search(r'group-title="([^"]*)"', extinf)
            if keep_all or allowed(m.group(1) if m else "", line):
                out += [extinf, line]
            extinf = ""
    return out


# ---- sources ------------------------------------------------------------------

def make_source(country: str, catalog: bool) -> str:
    """'freetv:brazil?catalog=1' — what is stored as the playlist source."""
    return f"{SCHEME}{country}?catalog={int(catalog)}"


def is_free_tv(source: str) -> bool:
    return source.startswith(SCHEME)


def parse_source(source: str) -> tuple[str, bool]:
    country, _sep, query = source[len(SCHEME):].partition("?")
    catalog = parse_qs(query).get("catalog", ["1"])[0] != "0"
    return country, catalog


def channels_url(country: str) -> str:
    if country == ALL:
        return FREE_TV_URL + "playlist.m3u8"
    return f"{FREE_TV_URL}playlists/playlist_{country}.m3u8"


def build_playlist(source: str, read: Callable[[str], str]) -> str:
    """The combined M3U text: the curated catalog, then the filtered channels."""
    country, catalog = parse_source(source)
    entries: list[str] = []
    if catalog:
        entries += filter_playlist(CATALOG.read_text(encoding="utf-8"), keep_all=True)
    if country:
        entries += filter_playlist(read(channels_url(country)))
    # The same stream may be in both lists (e.g. TV Câmara): keep the first.
    lines = ["#EXTM3U"]
    seen: set[str] = set()
    for extinf, url in zip(entries[::2], entries[1::2], strict=True):
        if url not in seen:
            seen.add(url)
            lines += [extinf, url]
    return "\n".join(lines) + "\n"


# ---- group names ----------------------------------------------------------------

_CATALOG_GROUPS = {
    "Open TV": lambda: _("Open TV"),
    "Movies – Blender Studio": lambda: _("Blender Studio"),
    "Movies – Classics": lambda: _("Classics"),
    "Series – Cartoons": lambda: _("Cartoons"),
}
_THEMES = {
    "News": lambda: _("News"), "Business": lambda: _("Business"),
    "Weather": lambda: _("Weather"), "Documentaries": lambda: _("Documentaries"),
    "Music": lambda: _("Music"),
}
_GROUP_SLUGS = {"uk": "uk", "usa": "usa", "日本 / japan": "japan"}


def localize_groups(channels: list) -> None:
    """Group names in the user's language ("Brazil" -> "Brasil").

    Runs after parsing, so the media type is still detected from the original
    English group ("Movies – …").
    """
    names = country_names()
    cache: dict[str, str] = {}
    for ch in channels:
        if ch.group not in cache:
            cache[ch.group] = _group_name(ch.group, names)
        ch.group = cache[ch.group]


def _group_name(group: str, names: dict[str, str]) -> str:
    if group in _CATALOG_GROUPS:
        return _CATALOG_GROUPS[group]()
    key = group.casefold()
    slug = _GROUP_SLUGS.get(key, key.replace(" ", "_"))
    if slug in names:
        return names[slug]
    theme, _sep, suffix = group.partition(" (")  # "News (AR)"
    if theme in _THEMES:
        return _THEMES[theme]() + (f" ({suffix}" if suffix else "")
    return group


# ---- names --------------------------------------------------------------------

_ISO_JSON = Path("/usr/share/iso-codes/json/iso_3166-1.json")


def country_names() -> dict[str, str]:
    """Free-TV country slug -> name in the user's language (iso-codes)."""
    try:
        data = json.loads(_ISO_JSON.read_text(encoding="utf-8"))["3166-1"]
    except (OSError, ValueError, KeyError):
        data = []
    tr = gettext.translation("iso_3166-1", fallback=True).gettext
    by_code = {e["alpha_2"].lower(): tr(e.get("common_name") or e["name"]) for e in data}
    by_code.setdefault("xk", "Kosovo")  # not in ISO 3166-1
    return {slug: by_code.get(code) or slug.replace("_", " ").title()
            for slug, code in COUNTRIES.items()}


def default_country() -> str:
    """Free-TV slug of the user's country ("pt_BR.UTF-8" -> "brazil"), else ALL."""
    by_code = {code: slug for slug, code in COUNTRIES.items()}
    for value in (os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"),
                  os.environ.get("LANG"), locale.getlocale()[0]):
        if value and "_" in value:
            slug = by_code.get(value.split("_", 1)[1][:2].lower())
            if slug:
                return slug
    return ALL


def playlist_name(country: str, names: dict[str, str] | None = None) -> str:
    where = _("All countries") if country == ALL else (names or country_names()).get(
        country, country)
    return _("Free TV – {where}").format(where=where)
