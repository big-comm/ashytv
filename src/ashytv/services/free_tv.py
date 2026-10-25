"""Free TV: public playlists of free-to-air channels from the iptv-org project.

iptv-org (https://github.com/iptv-org/iptv) publishes community-maintained M3U
lists of channels that broadcasters make publicly available, one per country.
AshyTV only offers the links: it does not host or control any stream.
"""

from __future__ import annotations

import gettext
import json
import locale
import os
from pathlib import Path

from ..i18n import _

BASE_URL = "https://iptv-org.github.io/iptv/"
PROJECT_URL = "https://github.com/iptv-org/iptv"

ALL_BY_COUNTRY = "index.country"
ALL_BY_CATEGORY = "index.category"

# Countries with a playlist (iptv-org PLAYLISTS.md). iptv-org uses "uk" for the
# United Kingdom and "int" for international channels.
_CODES = """
ad ae af ag al am ao ar at au aw az ba bb bd be bf bg bh bi bj bn bo bq br bs bt
by bz ca cd cg ch ci cl cm cn co cr cu cv cw cy cz de dk do dz ec ee eg eh er es
et fi fr ge gf gg gh gm gn gp gq gr gt gu gy hk hn hr ht hu id ie il in int iq ir
is it jm jo jp ke kg kh km kn kp kr kw kz la lb lc li lk lt lu lv ly ma mc md me
mg mk ml mm mn mo mq mr mt mu mv mx my mz na nc ne ng ni nl no np nz om pa pe pf
pg ph pk pl pr ps pt py qa re ro rs ru rw sa sd se sg si sk sl sm sn so sr sv sx
sy td tg th tj tm tn tr tt tw tz ua ug uk us uy uz va vc ve vg vn ws xk ye za zw
"""
COUNTRIES = frozenset(_CODES.split())

_ISO_JSON = Path("/usr/share/iso-codes/json/iso_3166-1.json")
_ISO_TO_IPTV = {"gb": "uk"}


def playlist_url(code: str) -> str:
    """Country code ("br"), ALL_BY_COUNTRY or ALL_BY_CATEGORY -> M3U URL."""
    if code in (ALL_BY_COUNTRY, ALL_BY_CATEGORY):
        return f"{BASE_URL}{code}.m3u"
    return f"{BASE_URL}countries/{code}.m3u"


def is_free_tv(source: str) -> bool:
    return source.startswith(BASE_URL)


def country_names() -> dict[str, str]:
    """iptv-org code -> country name in the user's language (iso-codes)."""
    names: dict[str, str] = {}
    try:
        data = json.loads(_ISO_JSON.read_text(encoding="utf-8"))["3166-1"]
    except (OSError, ValueError, KeyError):
        data = []
    tr = gettext.translation("iso_3166-1", fallback=True).gettext
    for entry in data:
        code = _ISO_TO_IPTV.get(entry["alpha_2"].lower(), entry["alpha_2"].lower())
        if code in COUNTRIES:
            names[code] = tr(entry.get("common_name") or entry["name"])
    names["int"] = _("International")
    names.setdefault("xk", "Kosovo")  # not in ISO 3166-1
    for code in COUNTRIES - names.keys():  # iso-codes not installed
        names[code] = code.upper()
    return names


def default_country() -> str:
    """Country of the user's locale ("pt_BR.UTF-8" -> "br"), "" if unknown."""
    for value in (os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"),
                  os.environ.get("LANG"), locale.getlocale()[0]):
        if value and "_" in value:
            code = value.split("_", 1)[1][:2].lower()
            code = _ISO_TO_IPTV.get(code, code)
            if code in COUNTRIES:
                return code
    return ""
