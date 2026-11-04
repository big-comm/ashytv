"""AshyTV navigation sidebar: a rail of icons beside the page that opens over
it (labels, dimmed page behind) while the keyboard focus or the mouse is on
it — as the TV apps' side menu does."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from ..i18n import _

# (page key, label, icon) — None becomes a separator.
NAV_ITEMS = [
    ("home", _("Home"), "go-home-symbolic"),
    ("channels", _("Channels"), "tv-symbolic"),
    ("movies", _("Movies"), "ashy-film-symbolic"),
    ("series", _("Series"), "ashy-series-symbolic"),
    ("favorites", _("Favorites"), "ashy-heart-symbolic"),
    ("recordings", _("Recordings"), "ashy-record-symbolic"),
    None,
    ("lists", _("Playlists"), "view-list-symbolic"),
    ("settings", _("Settings"), "emblem-system-symbolic"),
]

FULL_WIDTH = 220
COMPACT_WIDTH = 72
_OPEN_MS = 220
_CLOSE_MS = 180


class NavSidebar(Gtk.Box):
    """Vertical navigation list highlighting the active item."""

    __gtype_name__ = "AshyNavSidebar"

    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ashy-nav")
        self._compact = False
        self._labels: list[Gtk.Label] = []

        self._buttons: dict[str, Gtk.Button] = {}
        for item in NAV_ITEMS:
            if item is None:
                sep = Gtk.Separator()
                sep.add_css_class("ashy-nav-sep")
                self.append(sep)
                continue
            key, label, icon = item
            btn = self._make_item(key, label, icon)
            self._buttons[key] = btn
            self.append(btn)

        self.append(Gtk.Box(vexpand=True))
        self._active = "home"
        self.set_compact(True)

    def _make_item(self, key: str, label: str, icon: str) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.append(Gtk.Image(icon_name=icon))
        text = Gtk.Label(label=label, xalign=0, hexpand=True)
        self._labels.append(text)
        box.append(text)

        # A click must not leave the focus here (the menu stays open while it has it).
        btn = Gtk.Button(child=box, tooltip_text=label, focus_on_click=False)
        btn.add_css_class("ashy-nav-item")
        btn.connect("clicked", lambda *_: self._select(key))
        return btn

    # ---- compact mode (the rail) ----------------------------------------

    @property
    def compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for label in self._labels:
            label.set_visible(not compact)
        if compact:
            self.add_css_class("compact")
        else:
            self.remove_css_class("compact")

    # ---- selection and focus -------------------------------------------

    def _select(self, key: str) -> None:
        self.set_active(key)
        self.emit("navigate", key)

    def set_active(self, key: str) -> None:
        self._active = key
        for k, btn in self._buttons.items():
            if k == key:
                btn.add_css_class("selected")
            else:
                btn.remove_css_class("selected")

    def focus_active(self) -> None:
        """Move the keyboard focus to the current page's item."""
        btn = self._buttons.get(self._active)
        if btn is not None:
            btn.grab_focus()

    def key_of(self, widget: Gtk.Widget | None) -> str | None:
        """The page whose item is (or contains) `widget`."""
        for key, btn in self._buttons.items():
            if widget is not None and (widget is btn or widget.is_ancestor(btn)):
                return key
        return None

    def has_focus_within(self, widget: Gtk.Widget | None) -> bool:
        return widget is not None and (widget is self or widget.is_ancestor(self))


class NavRail(Gtk.Overlay):
    """The page with the sidebar over it: closed, a rail of icons beside the
    page; open (focus or mouse on it), it widens over the page — the labels
    fade in and the page behind dims — and closes again when they leave."""

    __gtype_name__ = "AshyNavRail"

    def __init__(self, nav: NavSidebar, content: Gtk.Widget):
        super().__init__()
        self.nav = nav
        content.set_margin_start(COMPACT_WIDTH)
        self.set_child(content)

        self.scrim = Gtk.Box(can_target=False, opacity=0, visible=False)
        self.scrim.add_css_class("ashy-nav-scrim")
        self.add_overlay(self.scrim)

        # The menu is drawn at its full width and clipped to the rail.
        self.clip = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                       vscrollbar_policy=Gtk.PolicyType.NEVER,
                                       halign=Gtk.Align.START)
        self.clip.add_css_class("ashy-nav-clip")
        self.clip.set_child(nav)
        viewport = self.clip.get_child()
        if isinstance(viewport, Gtk.Viewport):
            viewport.set_scroll_to_focus(False)
        self.clip.set_size_request(COMPACT_WIDTH, -1)
        nav.set_size_request(COMPACT_WIDTH, -1)
        self.add_overlay(self.clip)

        self._width = float(COMPACT_WIDTH)
        self._open = False
        self._hover = False
        self._focus = False
        self._anim = Adw.TimedAnimation(widget=self.clip, value_from=COMPACT_WIDTH,
                                        value_to=FULL_WIDTH, duration=_OPEN_MS,
                                        target=Adw.CallbackAnimationTarget.new(self._step))
        self._anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._anim.connect("done", self._on_done)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self._set(hover=True))
        motion.connect("leave", lambda *_a: self._set(hover=False))
        self.clip.add_controller(motion)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_a: self._set(focus=True))
        focus.connect("leave", lambda *_a: self._set(focus=False))
        nav.add_controller(focus)

    @property
    def is_open(self) -> bool:
        return self._open

    def close(self) -> None:
        self._hover = False
        self._set(focus=False)

    def _set(self, hover: bool | None = None, focus: bool | None = None) -> None:
        if hover is not None:
            self._hover = hover
        if focus is not None:
            self._focus = focus
        want = self._hover or self._focus
        if want == self._open:
            return
        self._open = want
        if want:
            self.nav.set_compact(False)  # labels in (clipped until it widens)
            self.nav.set_size_request(FULL_WIDTH, -1)
            self.nav.add_css_class("open")
            self.scrim.set_visible(True)
        else:
            self.nav.remove_css_class("open")
        self._anim.pause()
        self._anim.set_value_from(self._width)
        self._anim.set_value_to(FULL_WIDTH if want else COMPACT_WIDTH)
        self._anim.set_duration(_OPEN_MS if want else _CLOSE_MS)
        self._anim.play()

    def _step(self, value: float) -> None:
        self._width = value
        self.clip.set_size_request(round(value), -1)
        self.clip.get_hadjustment().set_value(0)
        self.scrim.set_opacity((value - COMPACT_WIDTH) / (FULL_WIDTH - COMPACT_WIDTH))

    def _on_done(self, _anim) -> None:
        if not self._open:
            self.nav.set_compact(True)  # back to icons only
            self.nav.set_size_request(COMPACT_WIDTH, -1)
            self.scrim.set_visible(False)
