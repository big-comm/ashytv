"""Translation setup: resolve the locale dir, bind gettext and export ``_``.

Lookup order for compiled catalogs (``ashytv.mo``):
1. ``ASHYTV_LOCALE_DIR`` environment variable;
2. ``usr/share/locale`` inside the source tree (running via ``run.sh``);
3. the system default ``/usr/share/locale``.
"""

from __future__ import annotations

import gettext
import os
from collections.abc import Callable
from pathlib import Path

DOMAIN = "ashytv"


def _locale_dir() -> str:
    env = os.environ.get("ASHYTV_LOCALE_DIR")
    if env:
        return env
    source_tree = Path(__file__).resolve().parents[2] / "usr" / "share" / "locale"
    if source_tree.is_dir():
        return str(source_tree)
    return "/usr/share/locale"


LOCALE_DIR = _locale_dir()
gettext.bindtextdomain(DOMAIN, LOCALE_DIR)
gettext.textdomain(DOMAIN)

_: Callable[[str], str] = gettext.gettext
ngettext: Callable[[str, str, int], str] = gettext.ngettext
