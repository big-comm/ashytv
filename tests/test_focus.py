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
