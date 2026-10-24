"""EPG grid widgets: program row and time axis.

Children are positioned proportionally to their time within the window
[t0, t1), and a vertical line marks "now".
"""

from __future__ import annotations

import time

from gi.repository import Graphene, Gsk, Gtk

ROW_HEIGHT = 46


def _x(t: float, t0: float, t1: float, width: int) -> float:
    return (min(max(t, t0), t1) - t0) / max(1.0, t1 - t0) * width


class _TimeLayout(Gtk.Widget):
    """Base: children with (start, end) positioned in the time window."""

    def __init__(self):
        super().__init__()
        self._items: list[tuple[Gtk.Widget, float, float]] = []
        self.t0 = 0.0
        self.t1 = 1.0
        self.show_now = True
        self.add_css_class("ashy-now-line")

    def clear(self) -> None:
        for child, _s, _e in self._items:
            child.unparent()
        self._items.clear()

    def add_item(self, child: Gtk.Widget, start: float, stop: float) -> None:
        child.set_parent(self)
        self._items.append((child, start, stop))
        self.queue_allocate()

    def set_window(self, t0: float, t1: float) -> None:
        self.t0, self.t1 = t0, t1
        self.queue_allocate()
        self.queue_draw()

    def do_dispose(self) -> None:
        self.clear()
        super().do_dispose()

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 200, 600, -1, -1
        return ROW_HEIGHT, ROW_HEIGHT, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        for child, start, stop in self._items:
            x0 = _x(start, self.t0, self.t1, width)
            x1 = _x(stop, self.t0, self.t1, width)
            w = int(x1) - int(x0)
            min_w = child.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
            # Programs too short for the available space are hidden (no warnings).
            visible = w >= min_w and w > 0
            child.set_child_visible(visible)
            if not visible:
                continue
            min_h = child.measure(Gtk.Orientation.VERTICAL, w)[0]
            h = max(min_h, height)
            transform = Gsk.Transform().translate(Graphene.Point().init(int(x0), 0))
            child.allocate(w, h, -1, transform)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        for child, _s, _e in self._items:
            self.snapshot_child(child, snapshot)
        now = time.time()
        if self.show_now and self.t0 <= now < self.t1:
            x = _x(now, self.t0, self.t1, self.get_width())
            rect = Graphene.Rect().init(x - 1, 0, 2, self.get_height())
            # Color comes from CSS (.ashy-now-line), which follows the accent color.
            snapshot.append_color(self.get_color(), rect)


class TimelineRow(_TimeLayout):
    __gtype_name__ = "AshyTimelineRow"


class TimeAxis(_TimeLayout):
    """Time labels (every 30 min) aligned with the rows."""

    __gtype_name__ = "AshyTimeAxis"

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 200, 600, -1, -1
        return 28, 28, -1, -1

    def set_window(self, t0: float, t1: float) -> None:
        super().set_window(t0, t1)
        self.clear()
        step = 1800
        t = (int(t0) // step) * step
        while t < t1:
            if t >= t0:
                lbl = Gtk.Label(label=time.strftime("%H:%M", time.localtime(t)), xalign=0)
                lbl.add_css_class("ashy-epg-head")
                self.add_item(lbl, t, min(t1, t + step))
            t += step
