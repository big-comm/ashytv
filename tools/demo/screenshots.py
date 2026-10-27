#!/usr/bin/env python3
"""Generates the README screenshots using only the fictional demo server.

    python3 tools/demo/screenshots.py [output-dir]    # default: docs/screenshots
    ASHYTV_SCREENSHOT_LANG=de python3 tools/demo/screenshots.py /tmp/shots-de

Runs AshyTV against tools/demo/demo_server.py in an isolated profile (temporary
XDG directories, no system keyring), walks through the main screens and saves
PNGs rendered by GTK itself. Requires a graphical session.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"
PORT = 8765

# Isolated profile: must be set before GLib reads the directories.
_tmp = Path(tempfile.mkdtemp(prefix="ashytv-shots-"))
for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
    os.environ[var] = str(_tmp / var.lower())
# README screenshots are in English whatever the desktop language is;
# ASHYTV_SCREENSHOT_LANG=pt_BR (for example) previews a translation.
os.environ["LANGUAGE"] = os.environ.get("ASHYTV_SCREENSHOT_LANG", "en")
os.environ["LC_ALL"] = "en_US.UTF-8"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

import demo_server  # noqa: E402
from ashytv.application import AshyTvApplication  # noqa: E402
from ashytv.models import MediaType  # noqa: E402
from ashytv.services import secrets  # noqa: E402

# Nothing goes to the system keyring.
secrets.store_password = lambda *_a: False

app = AshyTvApplication()
# Separate instance: never talk to an AshyTV the user already has open.
app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
steps: list = []


def win():
    return app.window


def shot(name: str) -> None:
    w = win()
    width, height = w.get_width(), w.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(w).snapshot(snap, width, height)
    node = snap.to_node()
    if node is None:
        print("empty snapshot:", name)
        return
    tex = w.get_native().get_renderer().render_texture(
        node, Graphene.Rect().init(0, 0, width, height))
    OUT.mkdir(parents=True, exist_ok=True)
    tex.save_to_png(str(OUT / f"{name}.png"))
    print("saved:", OUT / f"{name}.png")


def step(delay_ms: int = 800):
    def deco(fn):
        steps.append((fn, delay_ms))
        return fn
    return deco


def run_next() -> bool:
    if not steps:
        app.quit()
        return False
    fn, delay = steps.pop(0)

    def go():
        fn()
        return False

    GLib.timeout_add(delay, go)
    return False


def wait(cond, then, timeout: float = 120) -> None:
    start = time.time()

    def poll():
        try:
            done = cond()
        except Exception as exc:  # keep polling: an exception would stop the loop
            print("wait condition failed:", exc)
            done = False
        if done or time.time() - start > timeout:
            then()
            return False
        return True

    GLib.timeout_add(250, poll)


def after(ms: int, fn) -> None:
    GLib.timeout_add(ms, lambda: (fn(), False)[1])


# ---- script ----------------------------------------------------------------

@step(1500)
def welcome():
    shot("welcome")
    win().welcome.emit("start")
    run_next()


@step(800)
def add_list():
    lists = win().pages["lists"]
    lists._tabs["xtream"].set_active(True)
    lists.name_entry.set_text("Demo")
    lists.host_entry.set_text(f"http://127.0.0.1:{PORT}")
    lists.user_entry.set_text("demo")
    lists.pass_entry.set_text("demo")
    lists._save()
    wait(lambda: not win()._importing and win()._epg_guide is not None, run_next)


@step(500)
def history():
    w = win()
    # Favorites and "continue watching" for the home screen.
    for ch in w.storage.channels(MediaType.LIVE, limit=4)[1:3]:
        w.storage.set_favorite(ch, True)
    movie = w.storage.channels(MediaType.MOVIE, order="added", limit=3)[2]
    w._play_channel(movie)

    def seek_and_close():
        w.player.mpv.seek(24, relative=False)
        after(2500, lambda: (w.player.close(), run_next()))

    wait(lambda: w.player.mpv.state == "playing", lambda: after(1500, seek_and_close), 30)


@step(500)
def live_history():
    w = win()
    ch = w.storage.channels(MediaType.LIVE, limit=1)[0]
    w._play_channel(ch)
    wait(lambda: w.player.mpv.state == "playing",
         lambda: after(2000, lambda: (w.player.close(), run_next())), 30)


def page(key: str, name: str, delay: int = 3500, before=None):
    @step(300)
    def _fn():
        win().navigate(key)
        if before:
            before()
        after(delay, lambda: (shot(name), run_next()))


page("home", "home", 5000)
page("channels", "channels")
page("channels", "guide", before=lambda: win().pages["channels"].set_view("guide"))
page("movies", "movies", 5000, before=lambda: win().pages["channels"].set_view("list"))
page("series", "series", 5000)


@step(300)
def series_detail():
    w = win()
    ch = w.storage.channels(MediaType.SERIES, order="added", limit=1)[0]
    w._open_detail(ch)
    after(4000, lambda: (shot("series-detail"), w.get_visible_dialog().close(), run_next()))


@step(300)
def player():
    w = win()
    ch = w.storage.channels(MediaType.LIVE, limit=1)[0]
    w.navigate("channels")
    w.pages["channels"].emit("play", ch)

    def capture():
        w.player.mpv.set_pause(True)
        w.player._show_controls(keep=True)
        after(1200, channel_list)

    def channel_list():
        shot("player")
        w._on_key(None, Gdk.KEY_Return, 0, 0)  # Enter: channel list over the video
        after(1200, lambda: (shot("channel-list"), w.player.close(), run_next()))

    wait(lambda: w.player.mpv.state == "playing", lambda: after(3000, capture), 30)


page("lists", "lists", 1500)


@step(300)
def settings():
    w = win()
    w.navigate("settings")
    cats = w.pages["settings"].cats
    cats.select_row(cats.get_row_at_index(1))
    after(1500, lambda: (shot("settings"), run_next()))


def main() -> None:
    demo_server.serve(port=PORT, background=True)
    app.connect("activate", lambda *_: GLib.timeout_add(1200, run_next))
    app.run([sys.argv[0]])


if __name__ == "__main__":
    main()
