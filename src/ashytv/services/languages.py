"""Language codes for audio/subtitle tracks (ISO 639, from iso-codes).

mpv reports tracks as "por", "eng", "pt-BR"…: these helpers turn them into
names in the user's language and give mpv the user's preferred codes.
"""

from __future__ import annotations

import gettext
import json
import locale
import os
from functools import cache
from pathlib import Path

_ISO_JSON = Path("/usr/share/iso-codes/json/iso_639-2.json")


@cache
def _table() -> dict[str, dict]:
    """Every code (2-letter, 3-letter, bibliographic) -> its ISO entry."""
    try:
        entries = json.loads(_ISO_JSON.read_text(encoding="utf-8"))["639-2"]
    except (OSError, ValueError, KeyError):
        entries = []
    table: dict[str, dict] = {}
    for e in entries:
        for key in ("alpha_2", "alpha_3", "bibliographic"):
            if e.get(key):
                table[e[key].lower()] = e
    return table


def _user_language() -> str:
    """'pt_BR' from LANGUAGE/LC_ALL/LC_MESSAGES/LANG (C/POSIX -> '')."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var, "").split(":")[0].split(".")[0]
        if value and value not in ("C", "POSIX"):
            return value
    return (locale.getlocale()[0] or "").split(".")[0]


def preferred_codes(lang: str | None = None) -> list[str]:
    """Codes mpv should prefer for the user: 'pt_BR' -> pt-BR, pt, por."""
    lang = lang if lang is not None else _user_language()
    if not lang:
        return []
    base, _sep, region = lang.replace("-", "_").partition("_")
    base = base.lower()
    codes = [f"{base}-{region.upper()}"] if region else []
    codes.append(base)
    entry = _table().get(base)
    if entry:
        codes += [entry[k] for k in ("alpha_3", "bibliographic") if entry.get(k)]
    if base == "pt" and region.upper() == "BR":
        codes.append("pob")  # common tag for Brazilian Portuguese tracks
    return list(dict.fromkeys(codes))


def language_name(code: str) -> str:
    """'por' / 'pt' / 'pt-BR' -> 'Portuguese' in the user's language."""
    if not code:
        return ""
    base, _sep, region = code.replace("_", "-").partition("-")
    entry = _table().get(base.lower())
    if entry is None:
        return code
    name = gettext.translation("iso_639-2", fallback=True).gettext(entry["name"])
    # "Portuguese; Brazilian"-style names: keep the first part.
    name = name.split(";")[0].strip()
    return f"{name} ({region.upper()})" if region else name
