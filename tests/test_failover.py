"""Backup servers: order, sticky choice, no failover on bad logins, anti-freeze
timings, health checks, storage and secure DNS."""

import io
import json
import socket
import sqlite3
import urllib.error

import pytest

from ashytv.services import doh, failover, importer
from ashytv.services.failover import (
    DOWN,
    NEXT_SERVER,
    ONLINE,
    RECONNECT,
    SLOW,
    AuthFailed,
    ServerPool,
    StallGuard,
)
from ashytv.services.storage import Storage
from ashytv.services.xtream_client import XtreamError, XtreamProvider

MAIN, B1, B2 = "http://main.example", "http://b1.example", "http://b2.example"
M3U = "#EXTM3U\n#EXTINF:-1 group-title=\"G\",A\nhttp://x/a.ts\n"


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b""))


@pytest.mark.parametrize("exc, expected", [
    (urllib.error.URLError("refused"), True),
    (TimeoutError(), True),
    (ConnectionResetError(), True),
    (http_error(503), True),
    (http_error(500), True),
    (http_error(404), True),
    (ValueError("not JSON"), True),
    (http_error(401), False),
    (http_error(403), False),
    (XtreamError("Invalid username or password"), False),
    (AuthFailed(), False),
    (KeyError("bug"), False),
])
def test_what_moves_to_another_server(exc, expected):
    assert failover.is_server_failure(exc) is expected


def test_pool_tries_in_order_and_sticks_to_the_one_that_worked():
    pool = ServerPool([MAIN, B1, B2])
    calls, switches = [], []

    def call(server):
        calls.append(server)
        if server != B2:
            raise urllib.error.URLError("down")
        return "ok"

    assert pool.run(call, lambda old, new: switches.append((old, new))) == "ok"
    assert calls == [MAIN, B1, B2]
    assert pool.active == 2
    assert switches == [(0, 2)]
    # Sticky: the next request starts with the server that worked.
    calls.clear()
    assert pool.run(call) == "ok"
    assert calls == [B2]
    assert pool.order() == [2, 0, 1]


def test_bad_login_is_not_retried_on_other_servers():
    pool = ServerPool([MAIN, B1])
    calls = []

    def call(server):
        calls.append(server)
        raise XtreamError("Invalid username or password")

    with pytest.raises(XtreamError):
        pool.run(call)
    assert calls == [MAIN]
    assert pool.active == 0


def test_all_servers_down_raises_the_last_error():
    pool = ServerPool([MAIN, B1], active=1)
    with pytest.raises(urllib.error.HTTPError):
        pool.run(lambda s: (_ for _ in ()).throw(http_error(502)))
    assert pool.active == 1  # nothing worked: keep the choice


def test_xtream_provider_builds_urls_on_the_server_in_use():
    prov = XtreamProvider(MAIN, "u", "p", backups=[B1])
    assert prov.stream_url("live/1.ts") == f"{MAIN}/live/u/p/1.ts"

    def call(p):
        if p.host == MAIN:
            raise urllib.error.URLError("down")
        return p.api_url("get_live_streams")

    assert prov.run(call).startswith(f"{B1}/player_api.php?")
    assert prov.active == 1
    assert prov.stream_url("live/1.ts") == f"{B1}/live/u/p/1.ts"
    assert prov.host == MAIN  # the playlist is still identified by its main server
    assert json.loads(prov.to_json())["host"] == MAIN


def test_backup_list_is_cleaned():
    urls = ["b1.example/", MAIN, "http://b1.example", "ftp://x", "", B2, "c", "d", "e"]
    assert failover.clean_backups(urls, MAIN) == [B1, B2, "http://c", "http://d"]


def test_swap_server_keeps_the_path():
    assert failover.swap_server(f"{MAIN}/movie/u/p/9.mp4", MAIN, B1) == f"{B1}/movie/u/p/9.mp4"
    assert failover.swap_server("http://other/x.ts", MAIN, B1) == "http://other/x.ts"


# ---- anti-freeze ----------------------------------------------------------------

def test_stall_guard_reconnects_once_then_moves_on():
    guard = StallGuard(stall_secs=8, window=120)
    assert guard.update(0, True) is None
    assert guard.update(7.9, True) is None
    assert guard.update(8.0, True) == RECONNECT
    guard.restarted()
    assert guard.update(10, False) is None
    # Frozen again soon after: the next server.
    assert guard.update(20, True) is None
    assert guard.update(28, True) == NEXT_SERVER
    # Much later, a single freeze is a reconnect again.
    assert guard.update(400, True) is None
    assert guard.update(408, True) == RECONNECT


def test_stall_guard_resets_when_playback_moves_or_a_new_stream_starts():
    guard = StallGuard(stall_secs=8)
    guard.update(0, True)
    guard.update(5, False)  # it played again: the timer starts over
    assert guard.update(9, True) is None
    assert guard.update(16, True) is None
    assert guard.update(17, True) == RECONNECT
    guard.new_stream()
    assert guard.failed(18) == RECONNECT  # history forgotten
    assert guard.failed(19) == NEXT_SERVER


# ---- health ---------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_health_check_status_and_latency():
    clock = Clock()

    def fast(_url):
        clock.t += 0.12

    def slow(_url):
        clock.t += 2.5

    def dead(_url):
        raise urllib.error.URLError("refused")

    def refused_login(_url):
        raise XtreamError("Invalid username or password")

    assert failover.check(MAIN, fast, clock).status == ONLINE
    assert failover.check(MAIN, fast, clock).latency_ms == 120
    assert failover.check(MAIN, slow, clock).status == SLOW
    assert failover.check(MAIN, dead, clock).status == DOWN
    assert failover.check(MAIN, refused_login, clock).status == ONLINE  # it answered


def test_probes_check_the_answer():
    xt = failover.xtream_probe("u", "p", lambda url: b'{"user_info": {"auth": 0}}')
    xt(MAIN)
    with pytest.raises(ValueError):
        failover.xtream_probe("u", "p", lambda url: b"<html>parked</html>")(MAIN)
    failover.m3u_probe(lambda url: b"\xef\xbb\xbf#EXTM3U\n#EXTINF")(MAIN)
    with pytest.raises(ValueError):
        failover.m3u_probe(lambda url: b"<html>")(MAIN)


def test_health_list_follows_the_server_list():
    stored = [{"url": B1, "status": "down", "latency_ms": 0, "checked": 5}, "junk"]
    health = failover.health_list(stored, [MAIN, B1])
    assert [(h.url, h.status) for h in health] == [(MAIN, ""), (B1, "down")]


# ---- storage and import ---------------------------------------------------------

def test_storage_keeps_servers_and_migrates(tmp_path):
    path = tmp_path / "t.db"
    db = Storage(path)
    pid = db.add_playlist("L", "src")
    db.set_servers(pid, [B1, B2], active=2)
    db.set_server_health(pid, [{"url": MAIN, "status": "down"}])
    assert db.backups(pid) == [B1, B2]
    assert db.active_server(pid) == 2
    assert db.server_health(pid)[0]["status"] == "down"
    db.close()
    raw = sqlite3.connect(path)
    for col in ("backups", "active_server", "health"):
        raw.execute(f"ALTER TABLE playlist DROP COLUMN {col}")
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()
    db = Storage(path)  # v6 -> v7
    assert db.backups(pid) == []
    assert db.active_server(pid) == 0


def test_m3u_import_uses_the_backup_url_and_remembers_it(tmp_path, monkeypatch):
    calls = []

    def read(source):
        calls.append(source)
        if source == "http://main.example/l.m3u":
            raise urllib.error.URLError("down")
        return M3U

    monkeypatch.setattr(importer, "read_source", read)
    db_path = tmp_path / "t.db"
    Storage(db_path).close()
    result = importer.import_m3u(db_path, "L", "http://main.example/l.m3u",
                                 backups=["http://b1.example/l.m3u"])
    db = Storage(db_path)
    assert result.count == 1
    assert db.active_server(result.playlist_id) == 1
    assert db.backups(result.playlist_id) == ["http://b1.example/l.m3u"]
    # An HTML page instead of a playlist also moves on (garbage).
    calls.clear()
    monkeypatch.setattr(importer, "read_source",
                        lambda s: "<html>" if "main" in s else M3U)
    importer.import_m3u(db_path, "L", "http://main.example/l.m3u",
                        backups=["http://b1.example/l.m3u"])


# ---- secure DNS --------------------------------------------------------------------

def answer(*ips, ttl=300):
    return json.dumps({"Status": 0, "Answer": [
        {"name": "x", "type": 5, "TTL": ttl, "data": "cname.example."},
        *({"name": "x", "type": 1, "TTL": ttl, "data": ip} for ip in ips)]}).encode()


def test_parse_answer_keeps_ipv4_addresses():
    assert doh.parse_answer(json.loads(answer("1.2.3.4", "5.6.7.8"))) == [
        ("1.2.3.4", 300), ("5.6.7.8", 300)]
    assert doh.parse_answer({"Status": 2}) == []
    assert doh.parse_answer({"Status": 0, "Answer": [{"type": 1, "data": "nope"}]}) == []


def test_resolver_uses_the_second_bootstrap_and_caches():
    clock, urls = Clock(), []

    def fetch(url, headers):
        urls.append((url, headers))
        if "1.1.1.1" in url:
            raise OSError("unreachable")
        return answer("9.9.9.9", ttl=120)

    r = doh.DohResolver(doh.CLOUDFLARE, fetch, clock)
    assert r.resolve("Server.Example.") == ["9.9.9.9"]
    assert [u for u, _h in urls] == [
        "https://1.1.1.1/dns-query?name=server.example&type=A",
        "https://1.0.0.1/dns-query?name=server.example&type=A"]
    assert urls[0][1] == {"Accept": "application/dns-json"}
    r.resolve("server.example")
    assert len(urls) == 2  # cached
    clock.t += 121
    r.resolve("server.example")
    assert len(urls) == 4  # expired (and 1.1.1.1 still fails first)


def test_resolver_failure_means_system_dns_for_a_while():
    clock, calls = Clock(), []

    def fetch(url, headers):
        calls.append(url)
        raise OSError("blocked")

    r = doh.DohResolver(doh.GOOGLE, fetch, clock)
    assert r.resolve("server.example") == []
    assert calls[0].startswith("https://8.8.8.8/resolve?")
    assert r.resolve("server.example") == []
    assert len(calls) == 2  # negative cache: no new queries
    assert r.resolve("127.0.0.1") == []
    assert r.resolve("localhost") == []
    assert r.resolve("printer.local") == []
    assert len(calls) == 2


def test_getaddrinfo_wrapper_falls_back_to_the_system(monkeypatch):
    seen = []

    def system(host, port, family=0, type=0, proto=0, flags=0):
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(doh, "_system_getaddrinfo", system)
    try:
        doh.configure(doh.CLOUDFLARE, lambda url, h: answer("10.0.0.7"))
        assert socket.getaddrinfo("server.example", 80)[0][4] == ("10.0.0.7", 80)
        assert doh.stream_target("http://server.example:8080/live/u/p/1.ts") == (
            "http://10.0.0.7:8080/live/u/p/1.ts", "server.example:8080")
        # https: the certificate must match the name, mpv keeps the system DNS
        assert doh.stream_target("https://server.example/x.m3u8") == (
            "https://server.example/x.m3u8", "")
        doh.configure(doh.CLOUDFLARE, lambda url, h: (_ for _ in ()).throw(OSError()))
        socket.getaddrinfo("other.example", 80)
        assert seen[-1] == "other.example"  # DoH failed: system resolver
        doh.configure(doh.SYSTEM)
        assert doh.active() is None
        assert doh.stream_target("http://server.example/x") == ("http://server.example/x", "")
        socket.getaddrinfo("third.example", 80)
        assert seen[-1] == "third.example"
    finally:
        doh.configure(doh.SYSTEM)
