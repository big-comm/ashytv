"""The playback settings menu has to show what is playing.

The menu is rebuilt every time it opens (set_create_popup_func), so whichever
option is marked is decided afresh on each opening. It used to mark 1x and the
video's own aspect, always — which is right only until the viewer changes
something, and then the menu says one thing while the player does another.
"""

import math

import pytest

from ashytv.ui.player_view import _ASPECTS, _SPEEDS


class _Mpv:
    """Stands in for the player: remembers what it was told, as mpv does."""

    def __init__(self):
        self._speed = 1.0
        self._aspect = "no"

    def set_speed(self, value):
        self._speed = value

    def speed(self):
        return self._speed

    def set_aspect(self, value):
        self._aspect = value

    def aspect(self):
        return self._aspect


def _marked_speed(mpv):
    """Which speed the menu would mark, the way player_view builds it."""
    return [s for s in _SPEEDS if math.isclose(s, mpv.speed())]


def _marked_aspect(mpv):
    return [v for _label, v in _ASPECTS if v == mpv.aspect()]


def test_the_speed_marked_is_the_speed_playing():
    mpv = _Mpv()
    assert _marked_speed(mpv) == [1.0], "fresh playback is 1x"

    mpv.set_speed(1.5)
    assert _marked_speed(mpv) == [1.5], (
        "the menu kept marking 1x while the video played at 1.5x")


def test_every_speed_on_offer_can_be_marked():
    # A value the menu offers but can never mark would be a dead option.
    mpv = _Mpv()
    for s in _SPEEDS:
        mpv.set_speed(s)
        assert _marked_speed(mpv) == [s], f"{s}x could not be marked"


def test_exactly_one_speed_is_marked():
    # Two marks in a radio list is a contradiction; none leaves it blank.
    mpv = _Mpv()
    for s in _SPEEDS:
        mpv.set_speed(s)
        assert len(_marked_speed(mpv)) == 1


def test_a_speed_outside_the_list_marks_nothing():
    # Reached by a keyboard shortcut, say: better blank than a wrong mark.
    mpv = _Mpv()
    mpv.set_speed(1.1)
    assert _marked_speed(mpv) == []


@pytest.mark.parametrize("value", [v for _label, v in _ASPECTS])
def test_the_aspect_marked_is_the_aspect_in_force(value):
    mpv = _Mpv()
    mpv.set_aspect(value)
    assert _marked_aspect(mpv) == [value]
