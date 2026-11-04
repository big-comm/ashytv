"""Favorites: everything the user marked in one place, as Jellyfin does — a
rail of channels, one of movies, one of series and, at the end, the people
(actors and directors) they follow."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services.storage import collapse_variants
from ...services.text import normalize
from ..cast import CastRow
from ._common import page_header
from .home_page import RailCards


class FavoritesPage(RailCards, Gtk.Box):
    __gtype_name__ = "AshyFavoritesPage"

    __gsignals__ = {
        # a channel to watch; a movie or series opens its details
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # a person (CastMember) to open
        "person": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # unused here (no "See more" cards), declared for RailCards
        "open-group": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self._guide = None
        self._dirty = True
        self._query = ""

        self.append(page_header(_("Favorites"),
                                _("Channels, movies, series and people you marked")))

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        inner.set_margin_start(28)
        inner.set_margin_end(28)
        inner.set_margin_bottom(24)
        self.live_box, self.live_row = self._section(inner, _("Channels"))
        self.movies_box, self.movies_row = self._section(inner, _("Movies"))
        self.series_box, self.series_row = self._section(inner, _("Series"))
        self.people = CastRow(_("People"))
        self.people.set_margin_top(10)
        self.people.connect("person", lambda _r, m: self.emit("person", m))
        inner.append(self.people)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(inner)

        self.empty = Adw.StatusPage(
            icon_name="ashy-heart-symbolic", title=_("Nothing here yet"), vexpand=True,
            description=_("Tap the heart on a channel, movie, series or person to "
                          "keep it here."))
        self.empty.add_css_class("compact")
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(sw, "rails")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

    # ---- API used by the window ------------------------------------------

    def set_guide(self, guide) -> None:
        self._guide = guide
        self.invalidate()

    def favorite_changed(self, _channel) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        self._dirty = True
        if self.get_mapped():
            self.refresh()

    def set_search(self, text: str) -> None:
        query = normalize(text.strip())
        if query != self._query:
            self._query = query
            self.invalidate()

    def focus_content(self) -> bool:
        return self.stack.get_visible_child().child_focus(Gtk.DirectionType.TAB_FORWARD)

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        live = self._match(self.storage.channels(MediaType.LIVE, favorites=True,
                                                 order="favorite"))
        if self.storage.group_variants:
            live = collapse_variants(live)
        movies = self._match(self.storage.channels(MediaType.MOVIE, favorites=True,
                                                   order="favorite"))
        series = self._match(self.storage.channels(MediaType.SERIES, favorites=True,
                                                   order="favorite"))
        people = [p for p in self.storage.favorite_people()
                  if not self._query or self._query in normalize(p.name)]
        self._fill(self.live_box, self.live_row, live, self._channel_card)
        self._fill(self.movies_box, self.movies_row, movies, self._poster_card)
        self._fill(self.series_box, self.series_row, series, self._poster_card)
        self.people.set_cast(people)
        anything = bool(live or movies or series or people)
        self.stack.set_visible_child_name("rails" if anything else "empty")
        if not anything and self._query:
            self.empty.set_title(_("No results"))
        else:
            self.empty.set_title(_("Nothing here yet"))

    def _match(self, items: list[Channel]) -> list[Channel]:
        if not self._query:
            return items
        return [c for c in items if self._query in normalize(c.display_name)]
