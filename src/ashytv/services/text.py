"""Text utilities (normalization for search)."""

from __future__ import annotations

import re
import unicodedata

from ..i18n import _

_EP_CODE_RE = re.compile(r"\bS\d{1,3}\s*E\d{1,4}\b", re.I)


def normalize(text: str) -> str:
    """Lowercase and accent-free: 'Crème Brûlée' -> 'creme brulee'."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def episode_title(raw: str, series_name: str, number: int) -> str:
    """'Starlight Bay 2026 - S01E0001' -> 'Episode 1'; keeps real titles."""
    title = raw.strip()
    base = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", series_name).strip()
    if base and title.lower().startswith(base.lower()):
        title = title[len(base):]
    title = _EP_CODE_RE.sub("", title).strip(" -–—:.|")
    return title or _("Episode {number}").format(number=number)
