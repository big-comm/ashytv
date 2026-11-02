"""Channel list over the video: the groups column (← again)."""

import time

import gi
import pytest

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from ashytv.models import Channel  # noqa: E402

if Gdk.Display.get_default() is None:  # pragma: no cover — headless CI
    pytest.skip("needs a display", allow_module_level=True)

from ashytv.ui.channel_overlay import GROUP_DELAY_MS, ChannelOverlay  # noqa: E402

GROUPS = [("\x01fav", "Favorites", 2), ("\x01recent", "Recently watched", 1),
          ("News", "News", 3), ("Sports", "Sports", 4)]


def chans(*names):
    return [Channel(name=n, url=f"u/{n}") for n in names]


def spin(seconds):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


@pytest.fixture
def overlay():
    win = Gtk.Window()
    ov = ChannelOverlay()
    win.set_child(ov)
    win.present()
    ov.show_channels(chans("A", "B"), 1, None, "News")
    yield ov
    win.destroy()


def test_groups_open_on_the_current_group_without_reloading(overlay):
    seen = []
    overlay.connect("group-selected", lambda _o, key: seen.append(key))
    overlay.show_groups(GROUPS, "News")
    assert overlay.groups_visible
    assert overlay.groups_sel.get_selected() == 2
    spin(GROUP_DELAY_MS / 1000 + 0.2)
    assert seen == []  # the list already shows News


def test_moving_through_groups_is_debounced(overlay):
    seen = []
    overlay.connect("group-selected", lambda _o, key: seen.append(key))
    overlay.show_groups(GROUPS, "News")
    overlay.groups_sel.set_selected(3)
    overlay.groups_sel.set_selected(0)
    overlay.groups_sel.set_selected(1)
    assert seen == []
    spin(GROUP_DELAY_MS / 1000 + 0.2)
    assert seen == ["\x01recent"]  # only where the user stopped


def test_opening_a_group_is_immediate_and_shows_its_channels(overlay):
    seen = []

    def load(ov, key):
        seen.append(key)
        ov.set_channels(chans("S1", "S2", "S3"), "Sports")

    overlay.connect("group-selected", load)
    overlay.show_groups(GROUPS, "News")
    overlay.open_group(3)
    assert seen == ["Sports"]
    assert overlay.store.get_n_items() == 3
    assert overlay.title.get_text() == "Sports"


def test_back_closes_the_groups_first_then_the_list(overlay):
    overlay.show_groups(GROUPS, "News")
    overlay.hide_groups()
    assert not overlay.groups_visible
    assert overlay.get_visible()
    overlay.show_groups(GROUPS, "News")
    overlay.hide()
    assert not overlay.get_visible()
    assert not overlay.groups_visible
