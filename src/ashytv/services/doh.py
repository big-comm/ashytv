"""Optional secure DNS (DNS over HTTPS) for the app's own connections.

Off by default. When on, host names of the app's requests (Xtream API, M3U,
EPG, images, cast data) are resolved through Cloudflare or Google over an
encrypted HTTPS connection instead of the system resolver — more private
and often more reliable. The resolver is reached by IP address (bootstrap),
so it doesn't need DNS itself, and any failure falls back to the system
resolver: the setting can never make a server unreachable.

It works by wrapping socket.getaddrinfo, which every Python connection
(urllib, http.client) uses; TLS still checks certificates against the real
host name. mpv has its own resolver: plain http:// streams can be given the
resolved address with the original Host header (see stream_target); https://
streams keep using the system DNS.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

SYSTEM, CLOUDFLARE, GOOGLE = "system", "cloudflare", "google"

# provider -> (bootstrap IPs, URL template, extra headers)
PROVIDERS: dict[str, tuple[tuple[str, ...], str, dict]] = {
    CLOUDFLARE: (("1.1.1.1", "1.0.0.1"), "https://{ip}/dns-query?{query}",
                 {"Accept": "application/dns-json"}),
    GOOGLE: (("8.8.8.8", "8.8.4.4"), "https://{ip}/resolve?{query}", {}),
}

_TIMEOUT = 4.0
_MIN_TTL, _MAX_TTL = 60, 3600
_FAIL_TTL = 30.0        # after a failure, use the system resolver for a while
_A = 1


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _skip(host: str) -> bool:
    """Names DoH must not touch: addresses, local names, single labels."""
    host = host.rstrip(".").lower()
    return (not host or _is_ip(host) or host == "localhost" or "." not in host
            or host.endswith((".local", ".localhost", ".lan", ".home.arpa")))


def parse_answer(data) -> list[tuple[str, int]]:
    """DNS JSON (Cloudflare/Google format) -> [(IPv4, ttl)]."""
    if not isinstance(data, dict) or data.get("Status") != 0:
        return []
    out = []
    for rec in data.get("Answer") or []:
        if not isinstance(rec, dict) or rec.get("type") != _A:
            continue
        ip = str(rec.get("data", ""))
        try:
            if isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address):
                out.append((ip, int(rec.get("TTL", _MIN_TTL) or _MIN_TTL)))
        except ValueError:
            continue
    return out


def _default_fetch(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AshyTV", **headers})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as resp:
        return resp.read(65536)


class DohResolver:
    """Resolves names with one DoH provider, with a small TTL cache."""

    def __init__(self, provider: str, fetch: Callable[[str, dict], bytes] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if provider not in PROVIDERS:
            raise ValueError(provider)
        self.provider = provider
        self._fetch = fetch or _default_fetch
        self._clock = clock
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, host: str) -> list[str]:
        """IPv4 addresses of `host`; [] means "ask the system resolver"."""
        host = host.rstrip(".").lower()
        if _skip(host):
            return []
        now = self._clock()
        with self._lock:
            hit = self._cache.get(host)
        if hit is not None and hit[0] > now:
            return list(hit[1])
        ips, ttl = self._query(host)
        expires = now + (min(max(ttl, _MIN_TTL), _MAX_TTL) if ips else _FAIL_TTL)
        with self._lock:
            self._cache[host] = (expires, ips)
        return list(ips)

    def _query(self, host: str) -> tuple[list[str], int]:
        bootstrap, template, headers = PROVIDERS[self.provider]
        query = urlencode({"name": host, "type": "A"})
        for ip in bootstrap:
            try:
                data = json.loads(self._fetch(template.format(ip=ip, query=query), headers))
            except (OSError, ValueError):
                continue  # this address of the provider failed: try the other
            answers = parse_answer(data)
            if answers:
                return [a for a, _t in answers], min(t for _a, t in answers)
            if isinstance(data, dict) and data.get("Status") == 0:
                break  # a valid "no address" answer: no need to ask again
        return [], 0


# ---- installation -----------------------------------------------------------------

_system_getaddrinfo = socket.getaddrinfo
_active: DohResolver | None = None
_installed = False
_lock = threading.Lock()


def configure(provider: str, fetch: Callable[[str, dict], bytes] | None = None) -> None:
    """Use `provider` (cloudflare | google) or the system resolver (anything else)."""
    global _active, _installed
    with _lock:
        _active = DohResolver(provider, fetch) if provider in PROVIDERS else None
        if not _installed:
            socket.getaddrinfo = _getaddrinfo
            _installed = True


def active() -> DohResolver | None:
    return _active


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    resolver = _active
    name = host.decode() if isinstance(host, bytes) else host
    if resolver is None or not isinstance(name, str) or family == socket.AF_INET6:
        return _system_getaddrinfo(host, port, family, type, proto, flags)
    ips = resolver.resolve(name)
    if not ips:
        return _system_getaddrinfo(host, port, family, type, proto, flags)
    out = []
    for ip in ips:
        out += _system_getaddrinfo(ip, port, socket.AF_INET, type, proto,
                                   flags | socket.AI_NUMERICHOST)
    return out


def stream_target(url: str) -> tuple[str, str]:
    """For mpv: (URL with the resolved address, original Host header) for a
    plain http:// stream; (url, "") when DoH is off, the URL is https:// (the
    certificate must match the name, so mpv keeps the system DNS) or the
    name could not be resolved."""
    resolver = _active
    parts = urlsplit(url)
    if resolver is None or parts.scheme != "http" or not parts.hostname:
        return url, ""
    ips = resolver.resolve(parts.hostname)
    if not ips:
        return url, ""
    netloc = ips[0] + (f":{parts.port}" if parts.port else "")
    if parts.username or parts.password:
        return url, ""  # credentials in the authority: leave it alone
    return urlunsplit(parts._replace(netloc=netloc)), parts.netloc
