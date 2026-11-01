"""Guided tour: the step machine and when it is offered."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from ashytv.ui.tour import TOUR_VERSION, Step, TourMachine, mark_seen, should_offer  # noqa: E402


def steps(*names):
    return [Step(n, "", None, lambda n=n: n) for n in names]


def test_walks_forward_and_back_skipping_hidden_steps():
    hidden = {"b", "d"}
    m = TourMachine(steps("a", "b", "c", "d", "e"), lambda s: s.title not in hidden)
    assert m.start().title == "a"
    assert m.progress() == (1, 3)
    assert m.next().title == "c"
    assert m.progress() == (2, 3)
    assert not m.is_last
    assert m.next().title == "e"
    assert m.is_last
    assert m.progress() == (3, 3)
    assert m.back().title == "c"
    assert m.back().title == "a"
    assert m.back().title == "a"  # nothing before the first step
    m.next()
    m.next()
    assert m.next() is None  # finished
    assert m.current is None


def test_nothing_to_show():
    m = TourMachine(steps("a"), lambda s: False)
    assert m.start() is None
    assert m.progress() == (1, 1)


class Settings(dict):
    def set(self, key, value):
        self[key] = value


def test_offered_once_per_version_and_only_with_playlists():
    s = Settings(tour_version=0)
    assert not should_offer(s, has_playlists=False)
    assert should_offer(s, has_playlists=True)
    mark_seen(s)
    assert s["tour_version"] == TOUR_VERSION
    assert not should_offer(s, has_playlists=True)
    s["tour_version"] = TOUR_VERSION - 1  # an older tour: the new one is offered
    assert should_offer(s, has_playlists=True)
