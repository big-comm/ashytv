"""Keep the keyboard focus where the user can see it.

Moving the focus with the keyboard (or a remote) must never leave it on
something off screen: every scrolled area around the focused widget scrolls
just enough to show it whole, with a small margin so its ring isn't cut.
"""

from __future__ import annotations

from gi.repository import Gtk

MARGIN = 10


def scroll_delta(start: float, size: float, view: float, margin: float = MARGIN) -> float:
    """How far to scroll so [start, start+size) fits in [0, view)."""
    if view <= 0:
        return 0.0
    if start < margin or size + 2 * margin >= view:
        return start - margin  # too far up/left, or too big: show its start
    end = start + size
    if end > view - margin:
        return end - (view - margin)
    return 0.0


def _apply(adj: Gtk.Adjustment | None, delta: float) -> None:
    if adj is None or not delta:
        return
    upper = adj.get_upper() - adj.get_page_size()
    adj.set_value(max(adj.get_lower(), min(upper, adj.get_value() + delta)))


def reveal(widget: Gtk.Widget) -> None:
    """Scroll every scrolled window around `widget` to show it."""
    scroller = widget.get_ancestor(Gtk.ScrolledWindow)
    while scroller is not None:
        ok, b = widget.compute_bounds(scroller)
        if ok:
            _apply(scroller.get_vadjustment(),
                   scroll_delta(b.get_y(), b.get_height(), scroller.get_height()))
            _apply(scroller.get_hadjustment(),
                   scroll_delta(b.get_x(), b.get_width(), scroller.get_width()))
        parent = scroller.get_parent()
        scroller = parent.get_ancestor(Gtk.ScrolledWindow) if parent is not None else None


def is_visible(widget: Gtk.Widget | None) -> bool:
    return (widget is not None and widget.get_mapped() and widget.is_drawable()
            and widget.get_width() > 0 and widget.get_height() > 0)
