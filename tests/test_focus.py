"""Keeping the keyboard focus visible: how far a scrolled area moves."""

import gi

gi.require_version("Gtk", "4.0")

from ashytv.ui.focus import scroll_delta  # noqa: E402


def test_visible_item_does_not_scroll():
    assert scroll_delta(start=50, size=40, view=300) == 0


def test_item_below_scrolls_until_it_fits_with_a_margin():
    assert scroll_delta(start=280, size=40, view=300, margin=10) == 30


def test_item_above_or_cut_at_the_top_scrolls_back():
    assert scroll_delta(start=-25, size=40, view=300, margin=10) == -35
    assert scroll_delta(start=4, size=40, view=300, margin=10) == -6


def test_item_bigger_than_the_view_shows_its_start():
    assert scroll_delta(start=120, size=400, view=300, margin=10) == 110
    assert scroll_delta(start=0, size=10, view=0) == 0


def test_keyboard_mode_on_with_keys_off_with_real_pointer_use():
    from ashytv.ui.focus import KeyboardMode

    mode = KeyboardMode()
    assert mode.motion(100, 100) is False      # first position: just remembered
    assert mode.key() is True                  # turned on
    assert mode.key() is False                 # already on
    assert mode.motion(100, 100) is False      # synthetic motion, same place
    assert mode.motion(103, 102) is False      # jitter below the threshold
    assert mode.active
    assert mode.motion(110, 100) is True       # the pointer really moved
    assert not mode.active
    mode.key()
    assert mode.pointer() is True              # a click or scroll
    assert mode.pointer() is False


def test_keyboard_mode_measures_motion_from_the_last_real_position():
    from ashytv.ui.focus import KeyboardMode

    mode = KeyboardMode()
    mode.motion(0, 0)
    mode.key()
    for x in (2, 4, 5):                        # creeping, each step small…
        assert mode.motion(x, 0) is False
    assert mode.motion(6, 0) is True           # …but 6 px from where it rested
