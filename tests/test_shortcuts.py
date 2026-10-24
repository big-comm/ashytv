"""Shortcut key caps."""

import gi

gi.require_version("Gtk", "4.0")
from ashytv.ui import shortcuts  # noqa: E402


def test_key_caps():
    assert shortcuts.key_caps("<Primary>f") == ["Ctrl", "F"]
    assert shortcuts.key_caps("Left") == ["←"]
    assert shortcuts.key_caps(shortcuts.DIGITS) == ["0–9"]
    assert shortcuts.key_caps("BackSpace") == ["Backspace"]
    assert shortcuts.key_caps("Escape")[0] in ("Esc", "Escape")  # GTK label, per locale


def test_every_accelerator_is_valid():
    from gi.repository import Gtk

    for _title, items in shortcuts.KEYBOARD:
        for accels, _label in items:
            for accel in shortcuts.dialog_accelerator(accels).split():
                assert Gtk.accelerator_parse(accel)[0], accel
