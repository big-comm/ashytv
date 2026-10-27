"""Spatial navigation: which target an arrow key picks."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from ashytv.ui.keynav import _Rect, _score  # noqa: E402

D = Gtk.DirectionType
FOCUS = _Rect(100, 100, 200, 140)


def test_only_targets_ahead_count():
    assert _score(FOCUS, _Rect(100, 200, 200, 240), D.DOWN) is not None
    assert _score(FOCUS, _Rect(100, 0, 200, 40), D.DOWN) is None
    assert _score(FOCUS, _Rect(300, 100, 400, 140), D.LEFT) is None


def test_sideways_moves_stay_on_the_same_line():
    assert _score(FOCUS, _Rect(300, 100, 400, 140), D.RIGHT) is not None
    # Up and to the right: not reachable with →
    assert _score(FOCUS, _Rect(300, 0, 400, 40), D.RIGHT) is None


def test_in_line_targets_win_over_closer_ones_to_the_side():
    below_far = _Rect(100, 600, 200, 640)
    below_side = _Rect(260, 160, 360, 200)
    assert _score(FOCUS, below_far, D.DOWN) < _score(FOCUS, below_side, D.DOWN)


def test_nearest_in_line_target_wins():
    near = _Rect(100, 160, 200, 200)
    far = _Rect(100, 400, 200, 440)
    assert _score(FOCUS, near, D.DOWN) < _score(FOCUS, far, D.DOWN)
