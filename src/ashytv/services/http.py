"""Shared HTTP (urllib) and cache directories."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from gi.repository import GLib

from .. import __version__
from ..i18n import _

USER_AGENT = f"AshyTV/{__version__} (+https://communitybig.org)"


def cache_dir(sub: str) -> Path:
    base = Path(GLib.get_user_cache_dir()) / "ashytv" / sub
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_root() -> Path:
    return Path(GLib.get_user_cache_dir()) / "ashytv"


def open_url(url: str, timeout: float = 30):
    """Open an http(s) URL with the app's User-Agent."""
    if not url.startswith(("http://", "https://")):
        raise ValueError(_("Unsupported URL: {url}").format(url=url[:40]))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Encoding": "identity"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_bytes(url: str, timeout: float = 30) -> bytes:
    with open_url(url, timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: float = 60):
    return json.loads(fetch_bytes(url, timeout).decode("utf-8", errors="replace"))
