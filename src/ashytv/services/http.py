"""Shared HTTP (urllib) and cache directories."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from gi.repository import GLib

from .. import APP_VERSION
from ..i18n import _

USER_AGENT = f"AshyTV/{APP_VERSION} (+https://communitybig.org)"


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


def describe_error(exc: BaseException) -> str:
    """User-facing description of a download/connection error."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 404):
            return _("The server refused the request (HTTP {code}). The account may have "
                     "expired or the login may be invalid.").format(code=exc.code)
        return _("The server returned an error (HTTP {code}).").format(code=exc.code)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return _("The server took too long to respond.")
    if isinstance(exc, urllib.error.URLError):
        return _("Could not reach the server: {reason}").format(reason=exc.reason)
    return str(exc) or exc.__class__.__name__
