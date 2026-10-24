"""AshyTV navigation sidebar (full or compact, icons only)."""

from __future__ import annotations

from gi.repository import GObject, Gtk

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


class NavSidebar(Gtk.Box):
    """Vertical navigation list highlighting the active item."""

    __gtype_name__ = "AshyNavSidebar"

    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "compact-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
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
        self.toggle = Gtk.Button(icon_name="sidebar-show-symbolic",
                                 tooltip_text=_("Collapse menu"))
        self.toggle.add_css_class("ashy-nav-item")
        self.toggle.add_css_class("ashy-nav-toggle")
        self.toggle.connect("clicked", lambda *_: self.set_compact(not self._compact))
        self.append(self.toggle)

        self._active = "home"

    def _make_item(self, key: str, label: str, icon: str) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.append(Gtk.Image(icon_name=icon))
        text = Gtk.Label(label=label, xalign=0, hexpand=True)
        self._labels.append(text)
        box.append(text)

        btn = Gtk.Button(child=box, tooltip_text=label)
        btn.add_css_class("ashy-nav-item")
        btn.connect("clicked", lambda *_: self._select(key))
        return btn

    # ---- compact mode --------------------------------------------------

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
        self.toggle.set_tooltip_text(_("Expand menu") if compact else _("Collapse menu"))
        self.emit("compact-changed", compact)

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

    def has_focus_within(self, widget: Gtk.Widget | None) -> bool:
        return widget is not None and (widget is self or widget.is_ancestor(self))
