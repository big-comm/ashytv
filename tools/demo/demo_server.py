#!/usr/bin/env python3
"""Fictional Xtream Codes server for development, demos and screenshots.

Everything it serves is generated on the fly: invented channel names,
procedural SVG logos/posters, a synthetic XMLTV guide and a video produced
with ffmpeg. No real content, brands or third-party artwork are involved.

    python3 tools/demo/demo_server.py            # http://127.0.0.1:8765
    # In AshyTV: Lists → Xtream Codes → server http://127.0.0.1:8765,
    # user "demo", password "demo".
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import random
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "media"
USER = PASSWORD = "demo"

# ---- catalog (all names are invented) -------------------------------------

LIVE_GROUPS = ["News", "Sports", "Free-to-air", "Kids", "Documentaries",
               "Music", "Movies 24/7"]
CHANNELS = [
    ("Aurelia News", "News"), ("Harbor Daily", "News"),
    ("Pulse 24", "News"), ("Nimbus Sports", "Sports"),
    ("Orbit Sports", "Sports"), ("Arena Max", "Sports"),
    ("Lumen One", "Free-to-air"), ("Breeze TV", "Free-to-air"), ("Horizon Central", "Free-to-air"),
    ("Pixelito Kids", "Kids"), ("Kiwi Kids", "Kids"),
    ("Terra Docs", "Documentaries"), ("Beacon Nature", "Documentaries"),
    ("Coralia Music", "Music"), ("Tidal Hits", "Music"),
    ("Vortex Cinema", "Movies 24/7"), ("Dawn Cinema", "Movies 24/7"),
    ("Studio Lume", "Movies 24/7"),
]
PROGRAMS = {
    "News": ["Morning Brief", "Afternoon Report", "World Today", "Markets Now",
             "Evening Edition", "Open Interview"],
    "Sports": ["Match Day Live", "Goal by Goal", "Arena Talk", "Full Round",
               "Grand Prix Replay", "Total Sport"],
    "Free-to-air": ["Coffee and Headlines", "Evening Drama", "Afternoon Show",
                    "Easy Kitchen", "Sunday at Home", "Film Club"],
    "Kids": ["Space Adventures", "Forest Friends", "Little Scientists",
             "Robot Pals", "Drawing Time"],
    "Documentaries": ["Wild Frontiers", "Deep Oceans", "Lost Cities",
                      "Planet in Motion", "Brilliant Minds"],
    "Music": ["Top 20", "Live Unplugged", "Late Night Classics", "Festival Sessions",
              "Playlist of the Day"],
    "Movies 24/7": ["Afternoon Feature", "Action Cinema", "Great Classics",
                    "Sci-Fi Marathon", "Family Comedy"],
}
MOVIE_GENRES = ["Action", "Adventure", "Comedy", "Drama", "Sci-Fi", "Animation",
                "Thriller"]
MOVIES = [
    "The Last Lighthouse", "Paper Moons", "Echoes of Tomorrow", "Silver Run",
    "Neon Harbor", "The Tidewater House", "Northbound", "The Final Summer",
    "Glass Garden", "Midnight Orchard", "Velvet Storm", "Route 77",
    "The Clockmaker's Daughter", "Distant Signals", "Copper Sky", "Azure Island",
    "Quiet Thunder", "Atlas of Dreams", "Little Comet", "Shadow Market",
    "Guardians of the Glen", "Iron Petals", "Blue Meridian", "Sunday Engines",
    "Frostline Express", "Mount Clearwater", "Wandering Lights", "The Fifth Key",
    "Crimson Tide Pools", "Orbital", "The Long Crossing", "Lantern Bay Rescue",
]
SERIES = [
    ("Paper Harbor", "Drama"), ("Orbit Station", "Sci-Fi"),
    ("The Harbor Detectives", "Thriller"), ("Kitchen Kings", "Comedy"),
    ("Northern Lights", "Drama"), ("Hope Valley", "Drama"),
    ("Code Atlas", "Sci-Fi"), ("Wild Coast", "Adventure"),
    ("Little Inventors", "Animation"), ("Midnight Bureau", "Thriller"),
    ("The Sun Family", "Comedy"), ("The Glass Tower", "Drama"),
]


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))


def _palette(seed: str) -> tuple[str, str]:
    """Two colors (gradient) with a hue derived from the text."""
    hue = _rng(seed).random()

    def hexa(h, light, sat):
        r, g, b = colorsys.hls_to_rgb(h % 1.0, light, sat)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    return hexa(hue, 0.50, 0.70), hexa(hue + 0.12, 0.22, 0.65)


def logo_svg(name: str) -> str:
    c0, c1 = _palette(name)
    initials = "".join(w[0] for w in name.split()[:2]).upper()
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{c0}"/><stop offset="1" stop-color="{c1}"/></linearGradient></defs>
<circle cx="128" cy="128" r="120" fill="url(#g)"/>
<text x="128" y="152" text-anchor="middle" font-family="Noto Sans, sans-serif"
 font-weight="800" font-size="88" fill="#fff">{escape(initials)}</text></svg>"""


def _shapes(rng: random.Random, w: int, h: int) -> str:
    out = []
    for _ in range(rng.randint(3, 6)):
        kind = rng.choice(["circle", "ridge", "band"])
        op = rng.uniform(0.12, 0.35)
        if kind == "circle":
            out.append(f'<circle cx="{rng.uniform(0, w):.0f}" cy="{rng.uniform(0, h * .7):.0f}" '
                       f'r="{rng.uniform(w * .12, w * .45):.0f}" fill="#fff" opacity="{op:.2f}"/>')
        elif kind == "ridge":
            base = rng.uniform(h * .55, h * .85)
            pts = " ".join(f"{x:.0f},{base - rng.uniform(0, h * .3):.0f}"
                           for x in range(0, w + 60, 60))
            out.append(f'<polygon points="0,{h} {pts} {w},{h}" fill="#000" '
                       f'opacity="{op + .1:.2f}"/>')
        else:
            y = rng.uniform(0, h)
            out.append(f'<rect x="-20" y="{y:.0f}" width="{w + 40}" height="{rng.uniform(8, 40):.0f}" '
                       f'fill="#fff" opacity="{op / 2:.2f}" transform="rotate({rng.uniform(-25, 25):.0f} {w / 2} {y:.0f})"/>')
    return "\n".join(out)


def poster_svg(title: str, subtitle: str, wide: bool = False) -> str:
    w, h = (1280, 720) if wide else (500, 750)
    c0, c1 = _palette(title)
    rng = _rng(title)
    words = title.split()
    lines, cur = [], ""
    for word in words:  # wrap the title into up to 3 lines
        if len(cur) + len(word) > (22 if wide else 13) and cur:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    lines.append(cur)
    size = 64 if wide else 58
    y0 = h - 70 - (len(lines) - 1) * size * 1.05 - (0 if wide else 30)
    text = "".join(
        f'<text x="{60 if wide else w / 2}" y="{y0 + i * size * 1.05:.0f}" '
        f'text-anchor="{"start" if wide else "middle"}" font-family="Noto Sans, sans-serif" '
        f'font-weight="900" font-size="{size}" fill="#fff">{escape(line.upper())}</text>'
        for i, line in enumerate(lines))
    sub = (f'<text x="{60 if wide else w / 2}" y="{h - 40 if wide else h - 50}" '
           f'text-anchor="{"start" if wide else "middle"}" font-family="Noto Sans, sans-serif" '
           f'font-size="22" letter-spacing="4" fill="#fff" opacity=".75">{escape(subtitle.upper())}</text>')
    if wide:
        text = sub = ""  # backdrops stay clean (the app draws the title on top)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{c0}"/><stop offset="1" stop-color="{c1}"/></linearGradient>
<linearGradient id="f" x1="0" y1="0" x2="0" y2="1"><stop offset=".45" stop-color="#000" stop-opacity="0"/>
<stop offset="1" stop-color="#000" stop-opacity=".65"/></linearGradient></defs>
<rect width="{w}" height="{h}" fill="url(#g)"/>
{_shapes(rng, w, h)}
<rect width="{w}" height="{h}" fill="url(#f)"/>
{text}{sub}</svg>"""


# ---- Xtream data -----------------------------------------------------------

def _cats(names):
    return [{"category_id": str(i + 1), "category_name": n, "parent_id": 0}
            for i, n in enumerate(names)]


# Quality variants published per channel (cycled over CHANNELS).
QUALITIES = [("FHD", "HD", "SD"), ("HD",), ("FHD", "HD"), ("",)]


def build_catalog(base: str) -> dict:
    now = int(time.time())
    live = []
    for i, (name, group) in enumerate(CHANNELS):
        # Like real providers, some channels come in several qualities
        # ("NAME FHD", "NAME HD", "NAME SD"); AshyTV groups them.
        qualities = QUALITIES[i % len(QUALITIES)]
        for q, quality in enumerate(qualities):
            live.append({
                "num": len(live) + 1, "name": f"{name} {quality}".strip(),
                "stream_type": "live", "stream_id": 1000 + i * 10 + q,
                "stream_icon": f"{base}/img/logo/{100 + i}.svg",
                "epg_channel_id": f"ch{100 + i}.demo",
                "category_id": str(LIVE_GROUPS.index(group) + 1), "added": str(now)})
    movies = []
    for i, title in enumerate(MOVIES):
        rng = _rng(title)
        genre = MOVIE_GENRES[i % len(MOVIE_GENRES)]
        movies.append({
            "num": i + 1, "name": title, "stream_type": "movie", "stream_id": 500 + i,
            "stream_icon": f"{base}/img/poster/m{500 + i}.svg",
            "rating": round(rng.uniform(5.8, 9.4), 1), "year": str(rng.randint(2012, 2026)),
            "category_id": str(MOVIE_GENRES.index(genre) + 1),
            "container_extension": "ts", "added": str(now - i * 3600),
        })
    series = []
    for i, (title, genre) in enumerate(SERIES):
        rng = _rng(title)
        series.append({
            "num": i + 1, "name": title, "series_id": 900 + i,
            "cover": f"{base}/img/poster/s{900 + i}.svg",
            "backdrop_path": [f"{base}/img/backdrop/s{900 + i}.svg"],
            "plot": rng.choice(PLOTS), "genre": genre, "year": str(rng.randint(2015, 2026)),
            "rating": str(round(rng.uniform(6.5, 9.6), 1)),
            "category_id": str(MOVIE_GENRES.index(genre) + 1),
            "last_modified": str(now - i * 7200),
        })
    return {"live": live, "movies": movies, "series": series}


PLOTS = [
    "An unlikely crew must cross the country before the last train leaves.",
    "In a coastal town, old secrets resurface as a storm rolls in.",
    "Two childhood friends reopen the family workshop and find a forgotten map.",
    "On an orbital station, routine changes when an unknown signal is picked up.",
    "A determined chef tries to save the neighborhood restaurant in a single season.",
]


def series_info(item: dict) -> dict:
    eps = {}
    for season in (1, 2):
        eps[str(season)] = [{
            "id": f"{item['series_id']}{season}{n:02d}", "episode_num": str(n),
            "title": f"{item['name']} - S{season:02d}E{n:02d}",
            "container_extension": "ts", "season": season,
            "info": {"duration_secs": 2400 + n * 60,
                     "plot": f"Episode {n} of season {season}."},
        } for n in range(1, 7)]
    return {"info": {"name": item["name"], "plot": item["plot"]}, "episodes": eps}


def build_xmltv() -> bytes:
    now = int(time.time())
    start_day = now - now % 1800 - 6 * 3600
    out = ['<?xml version="1.0" encoding="utf-8"?><tv generator-info-name="ashytv-demo">']
    for i, (name, group) in enumerate(CHANNELS):
        cid = f"ch{100 + i}.demo"
        out.append(f'<channel id="{cid}"><display-name>{escape(name)}</display-name></channel>')
        rng = _rng(name)
        t = start_day - rng.choice([0, 900, 1800])
        titles = PROGRAMS[group]
        while t < now + 30 * 3600:
            dur = rng.choice([1800, 2700, 3600, 5400])
            title = rng.choice(titles)
            out.append(f'<programme start_timestamp="{t}" stop_timestamp="{t + dur}" '
                       f'channel="{cid}"><title>{escape(title)}</title>'
                       f'<desc>{escape(title)} — demo programming.</desc></programme>')
            t += dur
    out.append("</tv>")
    return "".join(out).encode()


# ---- media ------------------------------------------------------------------

VIDEO_SECONDS = 60


def ensure_video() -> Path:
    """Generate (once) a synthetic 60 s video with ffmpeg."""
    MEDIA.mkdir(parents=True, exist_ok=True)
    path = MEDIA / "demo.ts"
    if path.exists():
        return path
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required to generate the demo video")
    font = subprocess.run(["fc-match", "-f", "%{file}", "Sans:bold"],
                          capture_output=True, text=True).stdout.strip()
    draw = (f"drawtext=fontfile='{font}':text='AshyTV':fontsize=120:fontcolor=white"
            ":x=(w-text_w)/2:y=(h-text_h)/2-40,"
            f"drawtext=fontfile='{font}':text='demo stream':fontsize=40:"
            "fontcolor=white@0.7:x=(w-text_w)/2:y=(h/2)+70")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"gradients=s=1280x720:c0=0x0b1430:c1=0x1c56d6:c2=0x7c4dff:n=3:speed=0.02:d={VIDEO_SECONDS}:r=25",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:d={VIDEO_SECONDS}",
        "-vf", draw, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-af", "volume=0.05", "-f", "mpegts", str(path),
    ], check=True)
    return path


class Handler(BaseHTTPRequestHandler):
    catalog: dict = {}
    xmltv: bytes = b""
    video: Path | None = None

    def log_message(self, *_args) -> None:  # silent
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        self._send(json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")
        if url.path == "/player_api.php":
            return self._api(qs)
        if url.path == "/xmltv.php":
            return self._send(self.xmltv, "application/xml")
        if parts[0] == "img" and len(parts) == 3:
            kind, ident = parts[1], parts[2].removesuffix(".svg")
            return self._send(self._image(kind, ident).encode(), "image/svg+xml")
        if parts[0] in ("live", "movie", "series") and len(parts) == 4:
            if parts[1] != USER or parts[2] != PASSWORD:
                return self._send(b"forbidden", "text/plain", 403)
            # ASHYTV_DEMO_OFFLINE=1000,1011 simulates offline streams (fallback test).
            offline = os.environ.get("ASHYTV_DEMO_OFFLINE", "").split(",")
            if parts[3].split(".")[0] in offline:
                return self._send(b"offline", "text/plain", 404)
            return self._live() if parts[0] == "live" else self._stream()
        self._send(b"not found", "text/plain", 404)

    def _api(self, qs: dict) -> None:
        if qs.get("username") != USER or qs.get("password") != PASSWORD:
            return self._json({"user_info": {"auth": 0}})
        cat = self.catalog
        action = qs.get("action", "")
        if not action:
            return self._json({"user_info": {
                "username": USER, "auth": 1, "status": "Active", "is_trial": "1",
                "exp_date": str(int(time.time()) + 30 * 86400), "max_connections": "1"},
                "server_info": {"timestamp_now": int(time.time())}})
        responses = {
            "get_live_categories": _cats(LIVE_GROUPS),
            "get_vod_categories": _cats(MOVIE_GENRES),
            "get_series_categories": _cats(MOVIE_GENRES),
            "get_live_streams": cat["live"],
            "get_vod_streams": cat["movies"],
            "get_series": cat["series"],
        }
        if action == "get_series_info":
            item = next((s for s in cat["series"]
                         if str(s["series_id"]) == qs.get("series_id")), None)
            return self._json(series_info(item) if item else {})
        self._json(responses.get(action, []))

    def _image(self, kind: str, ident: str) -> str:
        if kind == "logo":
            return logo_svg(CHANNELS[int(ident) - 100][0])
        if ident.startswith("m"):
            item = next(m for m in self.catalog["movies"] if str(m["stream_id"]) == ident[1:])
            return poster_svg(item["name"], item["year"])
        item = next(s for s in self.catalog["series"] if str(s["series_id"]) == ident[1:])
        return poster_svg(item["name"], item["genre"], wide=(kind == "backdrop"))

    def _live(self) -> None:
        """Live channels are sent in real time, like a broadcast: a short
        burst, then only as fast as the video plays (no Content-Length)."""
        data = self.video.read_bytes()
        rate = len(data) / VIDEO_SECONDS          # bytes per second of video
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk = 16 * 1024
        sent = int(rate * 2)                       # ~2 s burst to start quickly
        try:
            self.wfile.write(data[:sent])
            started = time.monotonic()
            while sent < len(data):
                self.wfile.write(data[sent:sent + chunk])
                sent += chunk
                ahead = sent / rate - 2 - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(ahead)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player switched channel or closed

    def _stream(self) -> None:
        data = self.video.read_bytes()
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start = int(start_s or 0)
            end = int(end_s) if end_s else len(data) - 1
            chunk = data[start:end + 1]
            return self._send(chunk, "video/mp2t", 206, {
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(data)}",
                "Accept-Ranges": "bytes"})
        self._send(data, "video/mp2t", extra={"Accept-Ranges": "bytes"})


def serve(host: str = "127.0.0.1", port: int = 8765, background: bool = False):
    base = f"http://{host}:{port}"
    Handler.catalog = build_catalog(base)
    Handler.xmltv = build_xmltv()
    Handler.video = ensure_video()
    server = ThreadingHTTPServer((host, port), Handler)
    if background:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    print(f"AshyTV demo server on {base}  (user: {USER}, password: {PASSWORD})")
    server.serve_forever()
    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    serve(args.host, args.port)
