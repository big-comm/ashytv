"""The channel's schedule dialog, which is where a programme is set to record.

It used to be reachable only from the guide inside the Channels page, deep
enough in that the feature was reported as missing. These tests hold down
what the dialog offers, and for which programmes.
"""

import time
import types

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ashytv.models import Channel, MediaType  # noqa: E402
from ashytv.ui.schedule_dialog import schedule_dialog  # noqa: E402

Gtk.init()
Adw.init()

NOW = int(time.time())
# One programme already over, one on air, one still to come.
PASSADO = types.SimpleNamespace(title="Acabou", desc="", start=NOW - 7200, stop=NOW - 3600)
AGORA = types.SimpleNamespace(title="No ar", desc="", start=NOW - 600, stop=NOW + 600)
FUTURO = types.SimpleNamespace(title="Mais tarde", desc="", start=NOW + 3600, stop=NOW + 7200)


class _Book:
    """Stands in for RecordingBook/ReminderBook: only has() is consulted."""

    def __init__(self, starts=()):
        self.starts = set(starts)

    def has(self, playlist_id, channel_url, start):
        return start in self.starts


def _buttons(widget):
    """Every button under a widget, by its tooltip."""
    found = {}
    stack = [widget]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Button) and w.get_tooltip_text():
            found.setdefault(w.get_tooltip_text(), []).append(w)
        child = w.get_first_child() if hasattr(w, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def _rows(dialog):
    """The programme rows, keyed by title."""
    out = {}
    stack = [dialog.get_child()]
    while stack:
        w = stack.pop()
        if isinstance(w, Adw.ActionRow):
            out[w.get_title()] = w
        child = w.get_first_child() if hasattr(w, "get_first_child") else None
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


def _build(reminders=None, recordings=None, progs=(PASSADO, AGORA, FUTURO)):
    chamadas = []
    ch = Channel(id=1, playlist_id=1, name="Canal", url="http://x/1.ts",
                 media_type=MediaType.LIVE, tvg_id="t")
    guide = types.SimpleNamespace(between=lambda *_: list(progs))
    d = schedule_dialog(
        ch, guide, lambda c: None,
        on_remind=lambda c, p: chamadas.append(("remind", p.title)),
        on_record=lambda c, p: chamadas.append(("record", p.title)),
        reminders=reminders, recordings=recordings)
    return d, chamadas


def test_a_programme_still_to_come_can_be_recorded_or_remembered():
    d, _ = _build()
    acoes = _buttons(_rows(d)["Mais tarde"])
    assert "Record" in acoes
    assert "Remind me" in acoes


def test_a_programme_already_over_offers_nothing():
    # There is nothing left to record and nothing to be reminded of.
    d, _ = _build()
    assert _buttons(_rows(d)["Acabou"]) == {}


def test_a_programme_on_air_can_only_be_remembered():
    # Recording from the middle would save a piece of it, which is not what
    # "record this programme" means.
    acoes = _buttons(_rows(_build()[0])["No ar"])
    assert "Remind me" in acoes
    assert "Record" not in acoes and "Cancel recording" not in acoes


def test_what_is_already_set_says_so():
    d, _ = _build(reminders=_Book([AGORA.start]), recordings=_Book([FUTURO.start]))
    rows = _rows(d)
    assert "Cancel recording" in _buttons(rows["Mais tarde"])
    assert "Cancel reminder" in _buttons(rows["No ar"])
    # And the button is marked, not only labelled.
    assert _buttons(rows["Mais tarde"])["Cancel recording"][0].has_css_class("accent")


def test_pressing_record_reaches_the_window_with_that_programme():
    d, chamadas = _build()
    _buttons(_rows(d)["Mais tarde"])["Record"][0].emit("clicked")
    assert chamadas == [("record", "Mais tarde")]


def test_pressing_it_again_shows_the_opposite_state():
    d, _ = _build()
    btn = _buttons(_rows(d)["Mais tarde"])["Record"][0]
    assert not btn.has_css_class("accent")
    btn.emit("clicked")
    assert btn.has_css_class("accent")
    btn.emit("clicked")
    assert not btn.has_css_class("accent")


def test_without_the_callbacks_it_is_the_plain_listing_it_was():
    # The dialog is also built from places that do not own the books.
    ch = Channel(id=1, playlist_id=1, name="Canal", url="u",
                 media_type=MediaType.LIVE, tvg_id="t")
    guide = types.SimpleNamespace(between=lambda *_: [FUTURO])
    d = schedule_dialog(ch, guide, lambda c: None)
    assert _buttons(_rows(d)["Mais tarde"]) == {}
