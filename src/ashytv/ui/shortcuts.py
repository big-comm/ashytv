"""Keyboard and game controller shortcuts: one list for the whole app.

Used by the "Shortcuts" settings page (drawn as key caps) and by the
Ctrl+? shortcuts dialog, so both always show the same keys.
"""

from __future__ import annotations

from gi.repository import Gtk

from ..i18n import _

DIGITS = "digits"  # special token: "0–9"

# (section title, [(accelerators, description)]); each accelerator is an
# alternative way to do the same thing ("Left Right" = ← or →).
KEYBOARD: list[tuple[str, list[tuple[list[str], str]]]] = [
    (_("Navigation"), [
        (["<Primary>f"], _("Search (or just start typing)")),
        (["Left", "Right", "Up", "Down"], _("Move between the menu and the page")),
        (["Return"], _("Open the selected item")),
        (["BackSpace", "Escape"], _("Back to the menu")),
        (["Menu"], _("Group options (move, hide)")),
        (["<Primary>question"], _("Keyboard shortcuts")),
        (["<Primary>q"], _("Quit")),
    ]),
    (_("While watching"), [
        (["Return"], _("Channel list")),
        (["Up", "Down"], _("Previous/next channel")),
        ([DIGITS], _("Type a channel number")),
        (["BackSpace"], _("Last watched channel")),
        (["space"], _("Pause/resume (also pauses live TV)")),
        (["Left", "Right"], _("Rewind/forward 10 s")),
        (["f"], _("Fullscreen")),
        (["m"], _("Mute")),
        (["plus", "minus"], _("Volume")),
        (["Escape"], _("Exit fullscreen / close the player")),
    ]),
]

# Game controller buttons (labels, not accelerators).
GAMEPAD: list[tuple[list[str], str]] = [
    ([_("D-pad"), _("Stick")], _("Move")),
    (["A"], _("Open / confirm")),
    (["B"], _("Back")),
    (["X"], _("Pause/resume")),
    (["Y"], _("Channel list")),
    (["L1", "R1"], _("Previous/next channel")),
    (["Select"], _("Last watched channel")),
]

_ARROWS = {"Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}


def key_caps(accelerator: str) -> list[str]:
    """'<Primary>f' -> ['Ctrl', 'F'] (localized by GTK)."""
    if accelerator == DIGITS:
        return ["0–9"]
    if accelerator in _ARROWS:
        return [_ARROWS[accelerator]]
    if accelerator == "BackSpace":
        return [_("Backspace")]
    ok, key, mods = Gtk.accelerator_parse(accelerator)
    if not ok:
        return [accelerator]
    caps = [part for part in Gtk.accelerator_get_label(0, mods).split("+") if part]
    caps.append(Gtk.accelerator_get_label(key, 0))
    return caps


def keys_widget(alternatives: list[str], raw: bool = False) -> Gtk.Widget:
    """Key caps for each alternative, separated by a slash."""
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    for i, accel in enumerate(alternatives):
        if i:
            sep = Gtk.Label(label="/")
            sep.add_css_class("dim-label")
            box.append(sep)
        for cap in ([accel] if raw else key_caps(accel)):
            label = Gtk.Label(label=cap)
            label.add_css_class("ashy-keycap")
            box.append(label)
    return box


def dialog_accelerator(alternatives: list[str]) -> str:
    """Accelerator string for Adw.ShortcutsItem."""
    return " ".join("0" if a == DIGITS else a for a in alternatives)
