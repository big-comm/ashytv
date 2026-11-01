"""First-run guided tour: a spotlight over the real widgets with a small card.

`TourMachine` (GTK-free logic) walks the steps, skipping those whose widget
isn't on screen (narrow window, no live preview, nothing hidden yet…).
`Tour` shows it: it dims the window except the target's area and puts the
card next to it. ←/→ move, Esc closes. Nothing animates, so it also suits
"reduced motion".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

from ..i18n import _

# Bump when the tour gains steps worth showing again to people who saw it.
TOUR_VERSION = 1


def should_offer(settings, has_playlists: bool) -> bool:
    """Offer the tour once per version, when there is something to show."""
    return has_playlists and int(settings.get("tour_version") or 0) < TOUR_VERSION


def mark_seen(settings) -> None:
    settings.set("tour_version", TOUR_VERSION)


@dataclass(slots=True)
class Step:
    title: str
    text: str
    page: str | None                       # sidebar page to open first
    target: Callable[[], object | None]    # the widget to highlight (None: skip)
    prepare: Callable[[], None] | None = None


class TourMachine:
    """Which step is shown; `available(step)` says if it can be shown now."""

    def __init__(self, steps: list[Step], available: Callable[[Step], bool]):
        self.steps = steps
        self._available = available
        self.index = -1

    def start(self) -> Step | None:
        self.index = -1
        return self.next()

    def next(self) -> Step | None:
        for i in range(self.index + 1, len(self.steps)):
            if self._available(self.steps[i]):
                self.index = i
                return self.steps[i]
        self.index = len(self.steps)
        return None  # finished

    def back(self) -> Step | None:
        for i in range(self.index - 1, -1, -1):
            if self._available(self.steps[i]):
                self.index = i
                return self.steps[i]
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    @property
    def current(self) -> Step | None:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    def progress(self) -> tuple[int, int]:
        """(n, N) among the steps that can be shown."""
        shown = [i for i, s in enumerate(self.steps) if self._available(s)]
        n = sum(1 for i in shown if i <= self.index)
        return max(1, n), max(1, len(shown))

    @property
    def is_last(self) -> bool:
        return not any(self._available(s) for s in self.steps[self.index + 1:])


class _Spotlight(Gtk.Widget):
    """Dims everything but `hole` (window coordinates), with a ring around it."""

    __gtype_name__ = "AshyTourSpotlight"

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.hole: Graphene.Rect | None = None
        self.on_resize: Callable[[], None] | None = None

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # The window changed size: the target moved (after this layout pass).
        GLib.idle_add(self._resized)

    def _resized(self) -> bool:
        if self.on_resize is not None:
            self.on_resize()
        return GLib.SOURCE_REMOVE

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        dim = Gdk.RGBA()
        dim.parse("rgba(0,0,0,0.62)")
        hole = self.hole
        if hole is None:
            snapshot.append_color(dim, Graphene.Rect().init(0, 0, w, h))
            return
        x, y = hole.get_x(), hole.get_y()
        x2, y2 = x + hole.get_width(), y + hole.get_height()
        for rect in ((0, 0, w, y), (0, y2, w, h - y2), (0, y, x, y2 - y),
                     (x2, y, w - x2, y2 - y)):
            if rect[2] > 0 and rect[3] > 0:
                snapshot.append_color(dim, Graphene.Rect().init(*rect))
        ring = Gdk.RGBA()
        ring.parse("#62a0ea")
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(hole, 12)
        snapshot.append_border(rounded, [3, 3, 3, 3], [ring] * 4)


class Tour:
    """The tour over a window. `steps` are built by the window (it knows
    its pages); `finished()` is called when it closes."""

    PAD = 8
    CARD_W = 360

    def __init__(self, layer: Gtk.Overlay, steps: list[Step],
                 navigate: Callable[[str], None], finished: Callable[[], None]):
        self.layer = layer
        self.navigate = navigate
        self.finished = finished
        self.machine = TourMachine(steps, self._available)
        self.spot = _Spotlight()
        self.spot.on_resize = self._place
        self.card = self._build_card()
        self.active = False
        self._forward = True

    # ---- availability -------------------------------------------------------

    @staticmethod
    def _available(step: Step) -> bool:
        # Whether it is on screen is checked when the step is shown (its page
        # opens first); here only whether it exists at all.
        return step.target() is not None

    # ---- card ---------------------------------------------------------------

    def _build_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       halign=Gtk.Align.START, valign=Gtk.Align.START)
        card.add_css_class("ashy-tour-card")
        card.set_size_request(self.CARD_W, -1)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("ashy-section-title")
        self.text = Gtk.Label(xalign=0, wrap=True, max_width_chars=40)
        self.count = Gtk.Label(xalign=0, hexpand=True)
        self.count.add_css_class("ashy-faint")
        buttons = Gtk.Box(spacing=6)
        self.skip_btn = Gtk.Button(label=_("Skip"))
        self.skip_btn.add_css_class("flat")
        self.skip_btn.connect("clicked", lambda *_: self.stop())
        self.back_btn = Gtk.Button(label=_("Back"))
        self.back_btn.connect("clicked", lambda *_: self.back())
        self.next_btn = Gtk.Button(label=_("Next"))
        self.next_btn.add_css_class("suggested-action")
        self.next_btn.connect("clicked", lambda *_: self.next())
        buttons.append(self.count)
        buttons.append(self.skip_btn)
        buttons.append(self.back_btn)
        buttons.append(self.next_btn)
        card.append(self.title)
        card.append(self.text)
        card.append(buttons)
        return card

    # ---- flow ---------------------------------------------------------------

    def start(self) -> None:
        if self.active:
            return
        self.active = True
        self.layer.add_overlay(self.spot)
        self.layer.add_overlay(self.card)
        self._show(self.machine.start())

    def next(self) -> None:
        self._forward = True
        self._show(self.machine.next())

    def back(self) -> None:
        if self.machine.progress()[0] <= 1:
            return
        self._forward = False
        self._show(self.machine.back())

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self.layer.remove_overlay(self.card)
        self.layer.remove_overlay(self.spot)
        self.finished()

    def handle_key(self, keyval: int) -> bool:
        """Keys while the tour is open (it owns them all)."""
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if keyval == Gdk.KEY_Right or self.layer.get_root().get_focus() is None:
                self.next()
                return True
            return False  # Enter activates the focused card button
        if keyval == Gdk.KEY_Left:
            self.back()
        elif keyval == Gdk.KEY_Escape:
            self.stop()
        elif keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_space):
            return False  # move/activate between the card's buttons
        return True

    def _show(self, step: Step | None) -> None:
        if step is None:
            self.stop()
            return
        if step.page is not None:
            self.navigate(step.page)
        if step.prepare is not None:
            step.prepare()
        # Pages switch with a short crossfade: measure once it is done.
        GLib.timeout_add(220, self._after_switch, step)

    def _after_switch(self, step: Step) -> bool:
        if not self.active or self.machine.current is not step:
            return GLib.SOURCE_REMOVE
        widget = step.target()
        if widget is None or not widget.get_mapped():
            # Not on screen here (narrow window, nothing hidden…): skip it
            # in the direction the user is going.
            before = self.machine.index
            if self._forward:
                self.next()
            else:
                self._show(self.machine.back())
                if self.machine.index == before:
                    self.next()
            return GLib.SOURCE_REMOVE
        self.title.set_text(step.title)
        self.text.set_text(step.text)
        n, total = self.machine.progress()
        self.count.set_text(f"{n}/{total}")
        self.back_btn.set_sensitive(n > 1)
        self.next_btn.set_label(_("Done") if self.machine.is_last else _("Next"))
        self._place()
        self.next_btn.grab_focus()
        return GLib.SOURCE_REMOVE

    def _place(self) -> bool:
        """Hole around the target; the card below it, above it, or beside it."""
        step = self.machine.current
        widget = step.target() if step is not None and self.active else None
        if widget is None or not widget.get_mapped():
            return False
        ok, b = widget.compute_bounds(self.spot)
        if not ok:
            return False
        pad = self.PAD
        hole = Graphene.Rect().init(b.get_x() - pad, b.get_y() - pad,
                                    b.get_width() + 2 * pad, b.get_height() + 2 * pad)
        self.spot.hole = hole
        self.spot.queue_draw()
        w, h = self.spot.get_width(), self.spot.get_height()
        min_w = self.card.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        card_h = self.card.measure(Gtk.Orientation.VERTICAL, max(min_w, self.CARD_W))[1] + 32
        card_w = self.CARD_W + 36
        x2, y2 = hole.get_x() + hole.get_width(), hole.get_y() + hole.get_height()
        if y2 + card_h + 12 <= h:
            x, y = hole.get_x(), y2 + 12
        elif hole.get_y() - card_h - 12 >= 0:
            x, y = hole.get_x(), hole.get_y() - card_h - 12
        elif x2 + card_w + 12 <= w:
            x, y = x2 + 12, hole.get_y()
        else:
            x, y = hole.get_x() - card_w - 12, hole.get_y()
        x = int(min(max(12, x), max(12, w - card_w - 12)))
        y = int(min(max(12, y), max(12, h - card_h - 12)))
        self.card.set_margin_start(x)
        self.card.set_margin_top(y)
        return False
