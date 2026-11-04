"""Search from anywhere: the 🔍 on a page without a list of its own (Home,
Recordings, Playlists, Settings) opens this — channels, movies, series and
people that match, a rail each, as on Favorites."""

from __future__ import annotations

from gi.repository import GLib, Gtk

from ...i18n import _
from ...models import MediaType
from ...services.meta import people_query
from ...services.storage import collapse_variants
from .favorites_page import FavoritesPage

_MIN_CHARS = 2
_RAIL_LIMIT = 40
_DELAY_MS = 250          # typing pause before searching the catalog
_PEOPLE_DELAY_MS = 600   # and before asking the panel for people


class SearchPage(FavoritesPage):
    __gtype_name__ = "AshySearchPage"

    def __init__(self, storage):
        super().__init__(storage)
        # people_search(query, on_done(query, people)) is set by the window.
        self.people_search = None
        self._text = ""
        self._source = 0
        self._people_source = 0
        self._people_query = ""
        self._people: list = []
        header = self.get_first_child()
        title = header.get_first_child() if header is not None else None
        if isinstance(title, Gtk.Label):
            title.set_text(_("Search"))
        sub = title.get_next_sibling() if title is not None else None
        if isinstance(sub, Gtk.Label):
            sub.set_text(_("Channels, movies, series and people"))
        self.empty.set_icon_name("system-search-symbolic")

    def set_search(self, text: str) -> None:
        text = text.strip()
        if text == self._text:
            return
        self._text = text
        if self._source:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add(_DELAY_MS, self._run)
        query = people_query(text)
        if query != self._people_query:
            self._people_query = query
            self._people = []
            if self._people_source:
                GLib.source_remove(self._people_source)
                self._people_source = 0
            if query and self.people_search is not None:
                self._people_source = GLib.timeout_add(_PEOPLE_DELAY_MS, self._ask_people)

    def _run(self) -> bool:
        self._source = 0
        self.invalidate()
        return GLib.SOURCE_REMOVE

    def _ask_people(self) -> bool:
        self._people_source = 0
        if self.people_search is not None and self._people_query:
            self.people_search(self._people_query, self._on_people)
        return GLib.SOURCE_REMOVE

    def _on_people(self, query: str, people: list) -> None:
        if query == self._people_query:  # an older answer is dropped
            self._people = people
            self.people.set_cast(people)
            self._show_empty()

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        text = self._text
        searching = len(text) >= _MIN_CHARS

        def find(media_type: MediaType):
            if not searching:
                return []
            return self.storage.channels(media_type, search=text, order="name",
                                         limit=_RAIL_LIMIT * 3)

        live = find(MediaType.LIVE)
        if self.storage.group_variants:
            live = collapse_variants(live)
        self._fill(self.live_box, self.live_row, live[:_RAIL_LIMIT], self._channel_card)
        self._fill(self.movies_box, self.movies_row, find(MediaType.MOVIE)[:_RAIL_LIMIT],
                   self._poster_card)
        self._fill(self.series_box, self.series_row, find(MediaType.SERIES)[:_RAIL_LIMIT],
                   self._poster_card)
        self.people.set_cast(self._people if searching else [])
        self._show_empty()

    def _show_empty(self) -> None:
        anything = any(b.get_visible() for b in (self.live_box, self.movies_box,
                                                 self.series_box, self.people))
        self.stack.set_visible_child_name("rails" if anything else "empty")
        if len(self._text) < _MIN_CHARS:
            self.empty.set_title(_("Search"))
            self.empty.set_description(_("Type at least two letters."))
        else:
            self.empty.set_title(_("No results"))
            self.empty.set_description(_("Nothing matches “{text}”.").format(text=self._text))
