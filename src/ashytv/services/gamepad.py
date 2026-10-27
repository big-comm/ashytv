"""Game controller support (libmanette): drive the app like a TV remote.

Buttons and directions are translated into the same keys the keyboard uses,
so every screen that works with the keyboard works with a controller. When
libmanette is not installed the feature is simply unavailable.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

try:

    gi.require_version("Manette", "0.2")
    from gi.repository import Manette
except (ImportError, ValueError):  # pragma: no cover — optional dependency
    Manette = None

# Linux evdev button codes (linux/input-event-codes.h).
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR = 0x136, 0x137
BTN_SELECT, BTN_START = 0x13A, 0x13B
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x10, 0x11

BUTTON_KEYS = {
    BTN_SOUTH: Gdk.KEY_Return,       # A: confirm / open
    BTN_EAST: Gdk.KEY_Escape,        # B: back
    BTN_WEST: Gdk.KEY_space,         # X: play / pause
    BTN_NORTH: Gdk.KEY_Menu,         # Y: channel list
    BTN_TL: Gdk.KEY_Page_Up,         # L1: previous channel in the list
    BTN_TR: Gdk.KEY_Page_Down,       # R1: next channel
    BTN_SELECT: Gdk.KEY_BackSpace,   # Select: last watched channel
    BTN_START: Gdk.KEY_space,
    BTN_DPAD_UP: Gdk.KEY_Up,
    BTN_DPAD_DOWN: Gdk.KEY_Down,
    BTN_DPAD_LEFT: Gdk.KEY_Left,
    BTN_DPAD_RIGHT: Gdk.KEY_Right,
}
DIRECTION_KEYS = {Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right}
STICK_THRESHOLD = 0.6
REPEAT_DELAY_MS = 400
REPEAT_RATE_MS = 120


def button_key(code: int) -> int | None:
    return BUTTON_KEYS.get(code)


def axis_key(axis: int, value: float) -> tuple[int | None, bool]:
    """(key, pressed) for a hat or stick axis; key None when not handled.

    Returns which direction the axis points to; (key, False) means it was
    released back to the centre.
    """
    if axis in (ABS_HAT0X, ABS_X):
        neg, pos = Gdk.KEY_Left, Gdk.KEY_Right
    elif axis in (ABS_HAT0Y, ABS_Y):
        neg, pos = Gdk.KEY_Up, Gdk.KEY_Down
    else:
        return None, False
    threshold = 0.5 if axis in (ABS_HAT0X, ABS_HAT0Y) else STICK_THRESHOLD
    if value <= -threshold:
        return neg, True
    if value >= threshold:
        return pos, True
    return neg, False  # centred: releases whichever direction was held


class GamepadInput:
    """Listens to every connected controller and calls `on_key(keyval)`."""

    def __init__(self, on_key: Callable[[int], None]):
        self._on_key = on_key
        self._held: dict[int, int] = {}       # axis -> key held by that axis
        self._repeat_source = 0
        self._repeat_key = 0
        self.available = Manette is not None
        if not self.available:
            return
        self._monitor = Manette.Monitor.new()
        self._monitor.connect("device-connected", lambda _m, dev: self._watch(dev))
        it = self._monitor.iterate()
        while True:
            ok, device = it.next()
            if not ok:
                break
            self._watch(device)

    def _watch(self, device) -> None:
        device.connect("button-press-event", self._on_button)
        device.connect("button-release-event", self._on_button_release)
        device.connect("hat-axis-event", self._on_axis)
        device.connect("absolute-axis-event", self._on_axis)

    # ---- events --------------------------------------------------------

    def _on_button(self, _device, event) -> None:
        ok, code = event.get_button()
        key = button_key(code) if ok else None
        if key is not None:
            self._press(key)

    def _on_button_release(self, _device, event) -> None:
        ok, code = event.get_button()
        if ok and button_key(code) == self._repeat_key:
            self._stop_repeat()

    def _on_axis(self, _device, event) -> None:
        if event.get_event_type() == Manette.EventType.EVENT_HAT:
            ok, axis, value = event.get_hat()
        else:
            ok, axis, value = event.get_absolute()
        if not ok:
            return
        key, pressed = axis_key(axis, float(value))
        if key is None:
            return
        held = self._held.get(axis)
        if pressed and held != key:
            self._held[axis] = key
            self._press(key)
        elif not pressed and held is not None:
            del self._held[axis]
            if self._repeat_key == held:
                self._stop_repeat()

    # ---- key delivery with auto-repeat for directions --------------------

    def _press(self, key: int) -> None:
        self._on_key(key)
        self._stop_repeat()
        if key in DIRECTION_KEYS:
            self._repeat_key = key
            self._repeat_source = GLib.timeout_add(REPEAT_DELAY_MS, self._start_repeat)

    def _start_repeat(self) -> bool:
        self._repeat_source = GLib.timeout_add(REPEAT_RATE_MS, self._repeat)
        return GLib.SOURCE_REMOVE

    def _repeat(self) -> bool:
        self._on_key(self._repeat_key)
        return GLib.SOURCE_CONTINUE

    def _stop_repeat(self) -> None:
        if self._repeat_source:
            GLib.source_remove(self._repeat_source)
            self._repeat_source = 0
        self._repeat_key = 0
