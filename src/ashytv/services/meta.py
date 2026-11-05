"""Cast and filmographies from the AshyTV panel META API (TMDB data).

    GET {base}/api/v1/meta/title?type=movie|series&tmdb_id=&q=&year=&lang=
        -> {tmdb_id, type, title, cast: [{id, name, character, profile}]}
    GET {base}/api/v1/meta/person/{id}?lang=
        -> {id, name, profile, biography,
            credits: [{type, tmdb_id, title, original_title, year, poster}]}
    GET {base}/api/v1/meta/people?q=&lang=
        -> {items: [{id, name, profile, known_for}]}   (actors and directors)

The API is public (no credentials). 404 (meta_not_found) is remembered on
disk like an answer; 503 (meta_unavailable), 429 and network errors are not
cached and make the client stay offline for a few minutes, so the app falls
back to the provider's text cast without hammering the panel.

Everything here is GTK-free and safe to call from a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from ..models import Channel, MediaType
from .text import normalize

# The only place the panel address lives. ASHYTV_META_URL points the app at
# a local server for development and tests (tools/demo/demo_server.py).
META_BASE_URL = os.environ.get("ASHYTV_META_URL", "https://painel.ashytv.com").rstrip("/")

TITLE_TTL = 7 * 86400       # cast lists hardly change
PERSON_TTL = 7 * 86400
PEOPLE_TTL = 86400          # name searches (the panel caches them a day too)
PEOPLE_MIN, PEOPLE_MAX = 2, 80  # query length the panel accepts (else 400)
_MAX_PEOPLE = 20
MISS_TTL = 86400            # "not found" is asked again the next day
OFFLINE_SECS = 300          # after a 503/429/network error
_TIMEOUT = 15
_MAX_CAST = 30
_MAX_CREDITS = 400
_TMDB_IMG = "https://image.tmdb.org/t/p/w185"


class MetaError(Exception):
    """No cast data: the caller falls back to the provider's text cast."""


class MetaNotFound(MetaError):
    """The panel does not know this title/person (404)."""


class MetaUnavailable(MetaError):
    """The panel is down, rate-limited (503/429) or unreachable."""


@dataclass(slots=True)
class CastMember:
    name: str
    character: str = ""
    profile: str = ""       # image URL ("" = none)
    id: str = ""            # TMDB person id ("" = text cast only)


@dataclass(slots=True)
class Credit:
    type: MediaType          # MOVIE or SERIES
    title: str
    original_title: str = ""
    year: str = ""
    tmdb_id: str = ""
    poster: str = ""


@dataclass(slots=True)
class Person:
    name: str
    id: str = ""
    profile: str = ""
    biography: str = ""
    credits: list[Credit] = field(default_factory=list)


# ---- parsing (untrusted JSON) ----------------------------------------------

def _str(value, limit: int = 300) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(int(value)) if float(value).is_integer() else str(value)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _id(value) -> str:
    text = _str(value, 20)
    return text if text.isdigit() and text != "0" else ""


def image_url(value) -> str:
    """Web URL of an image ('/abc.jpg' TMDB paths are completed); other
    schemes (file:, data:…) are dropped."""
    text = _str(value, 500)
    if text.startswith("/") and not text.startswith("//"):
        return _TMDB_IMG + text
    return text if urlsplit(text).scheme in ("https", "http") else ""


def _year(value) -> str:
    m = re.search(r"(?:19|20)\d{2}", _str(value, 20))
    return m.group(0) if m else ""


def parse_title(data) -> list[CastMember]:
    if not isinstance(data, dict) or not isinstance(data.get("cast"), list):
        raise MetaNotFound("no cast")
    cast = []
    for item in data["cast"]:
        if not isinstance(item, dict):
            continue
        name = _str(item.get("name"), 120)
        if name:
            cast.append(CastMember(name=name, character=_str(item.get("character"), 120),
                                   profile=image_url(item.get("profile")),
                                   id=_id(item.get("id"))))
        if len(cast) >= _MAX_CAST:
            break
    return cast


def parse_person(data) -> Person:
    if not isinstance(data, dict) or not _str(data.get("name")):
        raise MetaNotFound("no person")
    credits = []
    for item in data.get("credits") if isinstance(data.get("credits"), list) else []:
        if not isinstance(item, dict):
            continue
        kind = {"movie": MediaType.MOVIE, "series": MediaType.SERIES,
                "tv": MediaType.SERIES}.get(_str(item.get("type")))
        title = _str(item.get("title"), 200)
        if kind is None or not title:
            continue
        credits.append(Credit(type=kind, title=title,
                              original_title=_str(item.get("original_title"), 200),
                              year=_year(item.get("year")), tmdb_id=_id(item.get("tmdb_id")),
                              poster=image_url(item.get("poster"))))
        if len(credits) >= _MAX_CREDITS:
            break
    return Person(name=_str(data.get("name"), 120), id=_id(data.get("id")),
                  profile=image_url(data.get("profile")),
                  biography=_str(data.get("biography"), 4000), credits=credits)


def parse_people(data) -> list[CastMember]:
    """/meta/people -> people with an id (`character` holds `known_for`:
    "Acting", "Directing"…)."""
    items = data.get("items") if isinstance(data, dict) else None
    people = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name, pid = _str(item.get("name"), 120), _id(item.get("id"))
        if name and pid:
            people.append(CastMember(name=name, character=_str(item.get("known_for"), 40),
                                     profile=image_url(item.get("profile")), id=pid))
        if len(people) >= _MAX_PEOPLE:
            break
    return people


def people_query(text: str) -> str:
    """The query sent to /meta/people, or "" when the panel would refuse it."""
    query = " ".join(text.split())
    return query if PEOPLE_MIN <= len(query) <= PEOPLE_MAX else ""


def split_cast(text: str) -> list[CastMember]:
    """Provider text cast ('A, B, C') -> names only, no duplicates."""
    names = [n.strip(" .") for n in re.split(r"[,;/|\n]", text or "")]
    unique = dict.fromkeys(n for n in names if 1 < len(n) <= 80)
    return [CastMember(name=n) for n in list(unique)[:_MAX_CAST]]


# ---- HTTP client with a disk cache -------------------------------------------

def _default_fetch(url: str) -> bytes:
    from . import http  # GLib import kept out of the pure helpers above
    return http.fetch_bytes(url, timeout=_TIMEOUT)


def _default_cache_dir() -> Path:
    from . import http
    return http.cache_dir("meta")


class MetaClient:
    """Thread-safe client. `fetch(url) -> bytes` raises urllib's HTTPError."""

    def __init__(self, base_url: str = META_BASE_URL, cache_dir: Path | None = None,
                 fetch: Callable[[str], bytes] | None = None,
                 clock: Callable[[], float] = time.time):
        self.base_url = base_url.rstrip("/")
        self._cache_dir = cache_dir
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._offline_until = 0.0
        self._lock = threading.Lock()

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir is None:
            self._cache_dir = _default_cache_dir()
        return self._cache_dir

    def title(self, kind: MediaType, tmdb_id: str = "", query: str = "",
              year: str = "", lang: str = "") -> list[CastMember]:
        params = {"type": "series" if kind is MediaType.SERIES else "movie"}
        if tmdb_id:
            params["tmdb_id"] = tmdb_id
        else:
            if not query.strip():
                raise MetaNotFound("no title")
            params["q"] = query.strip()
            if year:
                params["year"] = year
        if lang:
            params["lang"] = lang
        return parse_title(self._get("/api/v1/meta/title", params, TITLE_TTL))

    def backdrop(self, kind: MediaType, tmdb_id: str = "", query: str = "",
                 year: str = "", lang: str = "") -> str:
        """The title's landscape picture (w780) for the featured banner, or ""
        (same request and cache as `title`). The playlist's TMDB id first; when
        it finds no picture (a wrong id is common), the title search."""
        if tmdb_id:
            try:
                url = self._backdrop(kind, {"tmdb_id": tmdb_id}, lang)
            except MetaNotFound:
                url = ""
            if url or not query.strip():
                return url
        if not query.strip():
            raise MetaNotFound("no title")
        params = {"q": query.strip()}
        if year:
            params["year"] = year
        return self._backdrop(kind, params, lang)

    def _backdrop(self, kind: MediaType, params: dict, lang: str) -> str:
        params = {"type": "series" if kind is MediaType.SERIES else "movie", **params}
        if lang:
            params["lang"] = lang
        data = self._get("/api/v1/meta/title", params, TITLE_TTL)
        if isinstance(data, dict) and "backdrop" not in data:
            # Cached before the panel sent pictures: ask again (and update the cache).
            data = self._get("/api/v1/meta/title", params, -1)
        url = data.get("backdrop") if isinstance(data, dict) else None
        return url if isinstance(url, str) and url.startswith("https://") else ""

    def person(self, person_id: str, lang: str = "") -> Person:
        if not _id(person_id):
            raise MetaNotFound("no person id")
        params = {"lang": lang} if lang else {}
        return parse_person(self._get(f"/api/v1/meta/person/{quote(person_id)}",
                                      params, PERSON_TTL))

    def people(self, query: str, lang: str = "") -> list[CastMember]:
        """Actors and directors whose name matches, the most popular first."""
        query = people_query(query)
        if not query:
            return []
        params = {"q": query}
        if lang:
            params["lang"] = lang
        return parse_people(self._get("/api/v1/meta/people", params, PEOPLE_TTL))

    def _get(self, path: str, params: dict, ttl: int):
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(sorted(params.items()))
        cached = self._read_cache(url, ttl)
        if cached is not None:
            return self._answer(cached)
        with self._lock:
            if self._clock() < self._offline_until:
                raise MetaUnavailable("offline")
        try:
            body = self._fetch(url)
            data = json.loads(body.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._write_cache(url, {"status": 404})
                raise MetaNotFound("404") from None
            self._go_offline()
            raise MetaUnavailable(f"HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:  # URLError/timeout are OSErrors
            self._go_offline()
            raise MetaUnavailable(str(exc)) from None
        self._write_cache(url, {"status": 200, "data": data})
        return data

    def _go_offline(self) -> None:
        with self._lock:
            self._offline_until = self._clock() + OFFLINE_SECS

    @staticmethod
    def _answer(entry: dict):
        if entry.get("status") == 200:
            return entry.get("data")
        raise MetaNotFound("cached 404")

    def _path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:40] + ".json")

    def _read_cache(self, url: str, ttl: int) -> dict | None:
        try:
            entry = json.loads(self._path(url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict):
            return None
        max_age = ttl if entry.get("status") == 200 else MISS_TTL
        if self._clock() - float(entry.get("time", 0)) > max_age:
            return None
        return entry

    def _write_cache(self, url: str, entry: dict) -> None:
        path = self._path(url)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"time": self._clock(), **entry}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)


# ---- matching credits to the user's catalog ----------------------------------

# Provider decorations around a title: "[L]", "(Dublado)", "4K", "- 2024"…
_BRACKETS_RE = re.compile(r"[\[(][^\])]*[\])]")
_TAGS_RE = re.compile(r"\b(?:4k|uhd|fhd|hd|sd|1080p|720p|dublado|legendado|dub|leg)\b")
_TRAILING_YEAR_RE = re.compile(r"(?:^| )(?:19|20)\d{2}$")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_NON_WORD_RE = re.compile(r"[^0-9a-z]+")


def title_key(title: str) -> str:
    """'Duna: Parte Dois - 2024 [4K]' -> 'duna parte dois' (accents, the year
    at the end, quality tags and punctuation removed)."""
    text = _BRACKETS_RE.sub(" ", normalize(title)).replace("&", " and ")
    base = _NON_WORD_RE.sub(" ", _TAGS_RE.sub(" ", text)).strip()
    # "2012 (2009)" is a title made of a year: keep it when nothing else is left.
    return _TRAILING_YEAR_RE.sub("", base).strip() or base


def channel_year(ch: Channel) -> str:
    if ch.year:
        m = _YEAR_RE.search(ch.year)
        if m:
            return m.group(0)
    m = _YEAR_RE.search(ch.name)
    return m.group(0) if m else ""


def _years_close(a: str, b: str) -> bool:
    return not a or not b or abs(int(a) - int(b)) <= 1


class CatalogIndex:
    """Movies and series of the user's playlists, indexed for credit lookups.

    Built once per catalog version in a worker thread: tens of thousands of
    titles, then each lookup is a dictionary hit.
    """

    def __init__(self, channels: Iterable[Channel]):
        self.by_tmdb: dict[tuple[MediaType, str], list[Channel]] = {}
        self.by_title: dict[tuple[MediaType, str], list[Channel]] = {}
        self.by_key: dict[tuple[int, str], Channel] = {}
        for ch in channels:
            if ch.media_type not in (MediaType.MOVIE, MediaType.SERIES):
                continue
            self.by_key[(ch.playlist_id, ch.url)] = ch
            if ch.tmdb:
                self.by_tmdb.setdefault((ch.media_type, ch.tmdb), []).append(ch)
            key = title_key(ch.display_name)
            if key:
                self.by_title.setdefault((ch.media_type, key), []).append(ch)

    def find(self, credit: Credit) -> Channel | None:
        if credit.tmdb_id:
            found = self.by_tmdb.get((credit.type, credit.tmdb_id))
            if found:
                return found[0]
        for title in dict.fromkeys(t for t in (credit.title, credit.original_title) if t):
            for ch in self.by_title.get((credit.type, title_key(title)), ()):
                if ch.tmdb and credit.tmdb_id and ch.tmdb != credit.tmdb_id:
                    continue  # same name, another work (remake, homonym)
                if _years_close(channel_year(ch), credit.year):
                    return ch
        return None

    def match(self, credits: Iterable[Credit]) -> tuple[list[Channel], list[Channel]]:
        """The person's credits the user has: (movies, series), newest first,
        one entry per title even when several playlists carry it."""
        found: dict[tuple, tuple[str, Channel]] = {}
        for credit in credits:
            ch = self.find(credit)
            if ch is None:
                continue
            key = (ch.media_type, ch.tmdb or title_key(ch.display_name))
            if key not in found:
                found[key] = (credit.year or channel_year(ch), ch)
        ordered = sorted(found.values(), key=lambda yc: yc[0], reverse=True)
        movies = [ch for _y, ch in ordered if ch.media_type is MediaType.MOVIE]
        series = [ch for _y, ch in ordered if ch.media_type is MediaType.SERIES]
        return movies, series


def titles_with_cast(name: str, cast_texts: dict, lookup: Callable[[object], Channel | None]
                     ) -> tuple[list[Channel], list[Channel]]:
    """Fallback filmography: titles whose already-loaded provider info lists
    `name` in the cast. `cast_texts` maps a catalog key to its cast text and
    `lookup(key)` returns the catalog entry (None when gone)."""
    wanted = normalize(name).strip()
    if not wanted:
        return [], []
    movies, series, seen = [], [], set()
    for key, text in cast_texts.items():
        names = {normalize(m.name) for m in split_cast(text)}
        if wanted not in names or key in seen:
            continue
        seen.add(key)
        ch = lookup(key)
        if ch is None:
            continue
        (series if ch.media_type is MediaType.SERIES else movies).append(ch)
    return movies, series
