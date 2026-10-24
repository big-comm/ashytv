"""AshyTV navigation sidebar."""

from __future__ import annotations

from gi.repository import GObject, Gtk

from ..i18n import _

# (page key, label, icon) — None becomes a separator.
NAV_ITEMS = [
    ("home", _("Home"), "go-home-symbolic"),
    ("channels", _("Channels"), "tv-symbolic"),
    ("epg", _("Guide"), "x-office-calendar-symbolic"),
    ("movies", _("Movies"), "ashy-film-symbolic"),
    ("series", _("Series"), "ashy-series-symbolic"),
    ("favorites", _("Favorites"), "ashy-heart-symbolic"),
    ("recordings", _("Recordings"), "ashy-record-symbolic"),
    None,
    ("lists", _("Playlists"), "view-list-symbolic"),
    ("settings", _("Settings"), "emblem-system-symbolic"),
]


class NavSidebar(Gtk.Box):
    """Vertical navigation list highlighting the active item."""

    __gtype_name__ = "AshyNavSidebar"

    __gsignals__ = {
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("ashy-nav")

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

        self._active = "home"

    def _make_item(self, key: str, label: str, icon: str) -> Gtk.Button:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.append(Gtk.Image(icon_name=icon))
        box.append(Gtk.Label(label=label, xalign=0, hexpand=True))

        btn = Gtk.Button(child=box, tooltip_text=label)
        btn.add_css_class("ashy-nav-item")
        btn.connect("clicked", lambda *_: self._select(key))
        return btn

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
