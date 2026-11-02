"""A one-line label that scrolls its text when it doesn't fit (poster titles).

Normally it shows the ellipsized title. After the card has been hovered or
focused for a moment, an overflowing title scrolls back and forth until the
pointer/focus leaves. Only one label scrolls at a time.

It is cheap: each frame only moves the drawing offset of a Pango layout
(queue_draw), with no new size request or allocation.
"""

from __future__ import annotations

from gi.repository import GLib, Graphene, Gtk, Pango

START_DELAY_MS = 600
SPEED = 40.0        # pixels per second
PAUSE = 1.2         # seconds still at each end


def marquee_offset(elapsed: float, overflow: float, speed: float = SPEED,
                   pause: float = PAUSE) -> float:
    """Scroll offset after `elapsed` seconds: pause, scroll to the end,
    pause, scroll back, and again."""
    if overflow <= 0:
        return 0.0
    travel = overflow / speed
    t = elapsed % (2 * (pause + travel))
    if t < pause:
        return 0.0
    if t < pause + travel:
        return (t - pause) * speed
    if t < 2 * pause + travel:
        return overflow
    return overflow - (t - 2 * pause - travel) * speed


class MarqueeLabel(Gtk.Widget):
    """Draws its text with Pango directly (no child widgets): ellipsized when
    still, the full line shifted by an offset while scrolling."""

    __gtype_name__ = "AshyMarqueeLabel"

    _active: MarqueeLabel | None = None

    def __init__(self, width_chars: int = 14):
        super().__init__(accessible_role=Gtk.AccessibleRole.LABEL)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._chars = width_chars
        self._text = ""
        self._short: Pango.Layout | None = None
        self._full: Pango.Layout | None = None
        self._overflow = 0.0
        self._offset = 0.0
        self._timer = 0
        self._tick = 0
        self._started = 0

    # ---- text ------------------------------------------------------------

    def set_text(self, text: str) -> None:
        self.stop()
        if text == self._text:
            return
        self._text = text
        self._short = self._full = None
        self.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self.queue_resize()

    def get_text(self) -> str:
        return self._text

    def _layouts(self) -> tuple[Pango.Layout, Pango.Layout]:
        if self._short is None or self._full is None:
            self._full = self.create_pango_layout(self._text)
            self._short = self.create_pango_layout(self._text)
            self._short.set_ellipsize(Pango.EllipsizeMode.END)
        return self._short, self._full

    # ---- triggers --------------------------------------------------------

    def attach_hover(self, widget: Gtk.Widget) -> None:
        """Scroll while the pointer is over `widget` (the whole card)."""
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.start_soon())
        motion.connect("leave", lambda *_: self.stop())
        widget.add_controller(motion)

    def attach_focus(self, widget: Gtk.Widget) -> None:
        """Scroll while `widget` (or a child) has the keyboard focus."""
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.start_soon())
        focus.connect("leave", lambda *_: self.stop())
        widget.add_controller(focus)

    def start_soon(self, delay_ms: int = START_DELAY_MS) -> None:
        if self._timer or self._tick:
            return
        self._timer = GLib.timeout_add(delay_ms, self._begin)

    def _begin(self) -> bool:
        self._timer = 0
        if self._overflow < 1 or not self.get_mapped():
            return GLib.SOURCE_REMOVE
        active = MarqueeLabel._active
        if active is not None and active is not self:
            active.stop()
        MarqueeLabel._active = self
        self._started = 0
        self._tick = self.add_tick_callback(self._on_tick)
        return GLib.SOURCE_REMOVE

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0
            self.queue_draw()
        self._offset = 0.0
        if MarqueeLabel._active is self:
            MarqueeLabel._active = None

    @property
    def scrolling(self) -> bool:
        return bool(self._tick)

    def _on_tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if not self._started:
            self._started = now
        self._offset = marquee_offset((now - self._started) / 1e6, self._overflow)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    # ---- layout ----------------------------------------------------------

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, _for_size):
        full = self._layouts()[1]
        if orientation == Gtk.Orientation.HORIZONTAL:
            metrics = self.get_pango_context().get_metrics(None, None)
            width = metrics.get_approximate_char_width() * self._chars // Pango.SCALE
            return width, width, -1, -1
        height = full.get_pixel_size()[1]
        return height, height, -1, -1

    def do_size_allocate(self, width: int, _height: int, _baseline: int) -> None:
        self._short = self._full = None  # the font may have changed (CSS)
        short, full = self._layouts()
        short.set_width(width * Pango.SCALE)
        self._overflow = float(max(0, full.get_pixel_size()[0] - width))
        if self._overflow < 1 and self._tick:
            self.stop()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        short, full = self._layouts()
        color = self.get_color()
        y = (self.get_height() - full.get_pixel_size()[1]) / 2
        snapshot.save()
        snapshot.translate(Graphene.Point().init(-self._offset if self._tick else 0, y))
        snapshot.append_layout(full if self._tick else short, color)
        snapshot.restore()

    def do_unroot(self) -> None:
        self.stop()
        Gtk.Widget.do_unroot(self)


class FocusMarquee:
    """Scrolls the title of the focused item of a GridView/ListView.

    The view's items (not the cards inside them) take the keyboard focus, so
    a focus controller on the card never fires: follow the window's focus
    while the view is on screen. Cards keep their label in `_marquee`.
    """

    def __init__(self, view: Gtk.Widget):
        self._view = view
        self._window: Gtk.Widget | None = None
        self._handler = 0
        self._current: MarqueeLabel | None = None
        view.connect("map", self._on_map)
        view.connect("unmap", self._on_unmap)

    def _on_map(self, view) -> None:
        window = view.get_root()
        if window is not None and not self._handler:
            self._window = window
            self._handler = window.connect("notify::focus-widget", self._on_focus)

    def _on_unmap(self, _view) -> None:
        if self._handler and self._window is not None:
            self._window.disconnect(self._handler)
        self._handler = 0
        self._window = None
        self._set_current(None)

    def _on_focus(self, window, _pspec) -> None:
        focus = window.get_focus()
        label = None
        if focus is not None and focus.get_parent() is self._view:
            label = getattr(focus.get_first_child(), "_marquee", None)
        self._set_current(label)

    def _set_current(self, label: MarqueeLabel | None) -> None:
        if label is self._current:
            return
        if self._current is not None:
            self._current.stop()
        self._current = label
        if label is not None:
            label.start_soon()
