"""Shared HTTP (urllib) and cache directories."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

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


_local = threading.local()


def fetch_bytes_keepalive(url: str, timeout: float = 20) -> bytes:
    """fetch_bytes reusing one connection per host and thread.

    Many small downloads from the same server (posters, logos) skip a new
    TCP/TLS handshake each. Redirects and errors fall back to fetch_bytes.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return fetch_bytes(url, timeout)
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    key = (parts.scheme, parts.hostname, parts.port)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    for attempt in range(2):
        conn = conns.get(key)
        if conn is None:
            cls = (http.client.HTTPSConnection if parts.scheme == "https"
                   else http.client.HTTPConnection)
            conn = conns[key] = cls(parts.hostname, parts.port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT,
                                               "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException):
            conn.close()
            conns.pop(key, None)
            if attempt:
                raise
            continue  # the server closed an idle connection: reconnect once
        if resp.will_close:
            conn.close()
            conns.pop(key, None)
        if resp.status == 200:
            return data
        if 300 <= resp.status < 400:
            return fetch_bytes(url, timeout)  # let urllib follow the redirect
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
    raise AssertionError("unreachable")


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
