"""Welcome (hero) screen — first run."""

from __future__ import annotations

from pathlib import Path

from gi.repository import Gdk, GLib, GObject, Gtk

from .. import APP_ID
from ..i18n import _

_BG = Path(__file__).resolve().parent.parent / "data" / "images" / "welcome-bg.svg"


class WelcomeView(Gtk.Overlay):
    __gtype_name__ = "AshyWelcomeView"

    __gsignals__ = {
        "start": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()

        # Background: landscape filling the window (cover).
        bg = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        if _BG.exists():
            bg.set_paintable(Gdk.Texture.new_from_filename(str(_BG)))
        self.set_child(bg)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        content.set_margin_top(40)
        content.set_margin_bottom(40)
        content.set_margin_start(24)
        content.set_margin_end(24)

        # Logo: icon + "Ashy" "TV"
        brand = Gtk.Box(spacing=20, halign=Gtk.Align.CENTER)
        brand.append(Gtk.Image(icon_name=APP_ID, pixel_size=112))
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        name = Gtk.Box()
        ashy = Gtk.Label(label="Ashy")
        ashy.add_css_class("ashy-welcome-title")
        tv = Gtk.Label(label="TV")
        tv.add_css_class("ashy-welcome-title-accent")
        name.append(ashy)
        name.append(tv)
        sub = Gtk.Label(label=_("Your TV, your way"), xalign=0)
        sub.add_css_class("ashy-welcome-sub")
        words.append(name)
        words.append(sub)
        brand.append(words)
        content.append(brand)

        cards = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                            max_children_per_line=3, min_children_per_line=1,
                            column_spacing=16, row_spacing=16,
                            homogeneous=True, halign=Gtk.Align.CENTER)
        cards.set_margin_top(36)
        for icon, t, d in [
            ("tv-symbolic", _("Live Channels"), _("Watch your favorite channels")),
            ("ashy-film-symbolic", _("Movies & Series"), _("Complete entertainment")),
            ("view-list-symbolic", _("IPTV Playlists"), _("M3U and Xtream Codes support")),
        ]:
            cards.append(self._card(icon, t, d))
        # The cards are decorative: the keyboard goes straight to the button.
        cards.set_can_focus(False)
        content.append(cards)

        start = Gtk.Button(halign=Gtk.Align.CENTER)
        inner = Gtk.Box(spacing=8)
        inner.append(Gtk.Label(label=_("Get Started")))
        inner.append(Gtk.Image(icon_name="go-next-symbolic"))
        start.set_child(inner)
        start.add_css_class("pill-accent")
        start.set_size_request(260, -1)
        start.connect("clicked", lambda *_: self.emit("start"))
        content.append(start)
        self.start_btn = start
        self.connect("map", lambda *_: GLib.idle_add(lambda: start.grab_focus() and False))

        note = Gtk.Label(label=_("AshyTV is only a player: it does not host any content."))
        note.add_css_class("ashy-faint")
        content.append(note)

        self.add_overlay(content)

    def _card(self, icon: str, title: str, desc: str) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("ashy-glass")
        card.set_size_request(210, 140)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        valign=Gtk.Align.CENTER, vexpand=True)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.append(Gtk.Image(icon_name=icon, pixel_size=32))
        t = Gtk.Label(label=title)
        t.add_css_class("ashy-poster-title")
        d = Gtk.Label(label=desc, wrap=True, justify=Gtk.Justification.CENTER)
        d.add_css_class("ashy-page-subtitle")
        d.add_css_class("caption")
        inner.append(t)
        inner.append(d)
        card.append(inner)
        return card
