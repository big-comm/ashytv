"""Game controller button / axis mapping (no hardware needed)."""

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

from ashytv.services import gamepad as gp  # noqa: E402


def test_buttons_map_to_remote_keys():
    assert gp.button_key(gp.BTN_SOUTH) == Gdk.KEY_Return
    assert gp.button_key(gp.BTN_EAST) == Gdk.KEY_Escape
    assert gp.button_key(gp.BTN_TR) == Gdk.KEY_Page_Down
    assert gp.button_key(gp.BTN_DPAD_LEFT) == Gdk.KEY_Left
    assert gp.button_key(0x999) is None


def test_hat_and_stick_directions():
    assert gp.axis_key(gp.ABS_HAT0Y, -1) == (Gdk.KEY_Up, True)
    assert gp.axis_key(gp.ABS_HAT0X, 1) == (Gdk.KEY_Right, True)
    assert gp.axis_key(gp.ABS_HAT0X, 0)[1] is False          # back to centre
    assert gp.axis_key(gp.ABS_Y, 0.3)[1] is False           # inside the dead zone
    assert gp.axis_key(gp.ABS_Y, 0.9) == (Gdk.KEY_Down, True)
    assert gp.axis_key(0x05, 1.0) == (None, False)          # unhandled axis


def test_held_direction_repeats(monkeypatch):
    sources = []
    monkeypatch.setattr(gp.GLib, "timeout_add", lambda ms, fn: sources.append(fn) or 1)
    monkeypatch.setattr(gp.GLib, "source_remove", lambda _s: None)
    monkeypatch.setattr(gp, "Manette", None)
    keys = []
    pad = gp.GamepadInput(keys.append)
    pad._press(Gdk.KEY_Down)
    sources.pop(0)()          # repeat delay elapsed
    sources.pop(0)()          # one repeat tick
    assert keys == [Gdk.KEY_Down, Gdk.KEY_Down]
    pad._press(Gdk.KEY_Return)  # not a direction: no repeat scheduled
    assert keys[-1] == Gdk.KEY_Return
