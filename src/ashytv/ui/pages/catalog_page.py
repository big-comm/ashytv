"""Catalog page (Movies/Series) — groups (beside, folding) + poster grid."""

from __future__ import annotations

import re

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services.meta import people_query
from ..cast import CastRow
from ..channel_object import ChannelObject
from ..marquee import FocusMarquee, MarqueeLabel
from ._common import (
    GroupColumn,
    SearchFilter,
    add_context_menu,
    page_header,
    popup_menu,
    set_picture,
    update_favorite,
)

_PEOPLE_DELAY_MS = 600  # typing pause before asking the panel for people
_POSTER_W = 150
_POSTER_H = 225
# The poster plus the card's side margins: what one column actually takes.
_CARD_W = _POSTER_W + 12
# Titles handed to the grid at once when a group is opened: enough to fill
# several screens. The rest follow a page per turn of the main loop, so a
# group of ten thousand costs the same to open as one of fifty.
_FIRST_PAGE = 300
_NEXT_PAGE = 1500
# "Recently added" and "Top rated" are a shortlist, not a whole catalogue.
_SHORTLIST = 300
# How long the selection in the group column has to sit still before its
# titles are fetched. Walking down the list with the arrow keys crosses a
# dozen groups; loading each one on the way made the column crawl.
_GROUP_DELAY_MS = 170
# "FILMES - AÇÃO" -> "Ação"; "SERIES - DORAMAS" -> "Doramas"
_PREFIX_RE = re.compile(r"^\s*(filmes?|movies?|vod|s[ée]ries?)\s*[-|:/]\s*", re.I)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_HIDDEN = "__hidden__"
_SPECIAL = ("all", "new", "top", _HIDDEN)


def chip_label(group: str) -> str:
    label = _PREFIX_RE.sub("", group).strip() or group
    return label if not label.isupper() else label.title()


class CatalogPage(Gtk.Box):
    __gtype_name__ = "AshyCatalogPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "favorite-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # (media type, category): hide a category
        "hide-group": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "show-hidden": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # an actor or director found by the search (meta.CastMember)
        "show-person": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, storage, media_type: MediaType, title: str, subtitle: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self.media_type = media_type
        self._dirty = True
        self._chip = "all"
        self._load_run = 0
        self._group_source = 0

        self.append(page_header(title, subtitle))

        # Groups beside the posters (as on the TV apps), folding while the
        # focus is in the posters.
        body = Gtk.Box(spacing=14, vexpand=True)
        body.set_margin_start(28)
        body.set_margin_bottom(22)
        self.group_list = Gtk.ListBox()
        self.group_list.add_css_class("ashy-grouplist")
        self.group_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.group_list.connect("row-selected", self._on_group_selected)
        self.group_list.connect("row-activated", self._on_group_activated)
        self.groups = GroupColumn(self.group_list)
        self.groups.append_to(body)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.groups.watch(content)
        body.append(content)
        self.append(body)
        self._building = False

        # Actors and directors whose name matches the search (panel /meta/people).
        # people_search(query, on_done(query, people)) is set by the window.
        self.people_search = None
        self._people_query = ""
        self._people_source = 0
        self.people = CastRow(_("People"))
        self.people.set_margin_end(28)
        self.people.connect("person", lambda _r, m: self.emit("show-person", m))
        content.append(self.people)

        self.store = Gio.ListStore.new(ChannelObject)
        self.search = SearchFilter(self._update_empty)
        self.filter_model = Gtk.FilterListModel(model=self.store,
                                                filter=self.search.filter)
        selection = Gtk.NoSelection(model=self.filter_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", lambda _f, li: self._bound.discard(li))
        self._bound: set[Gtk.ListItem] = set()

        # max-columns is not only a cap on what is drawn: GridView builds a
        # card for thirty-odd rows of it, whatever the window's width. Left
        # at twelve it made 385 cards to show fifteen, and every change of
        # group paid for all of them. It is kept at the number of columns
        # the window can actually fit.
        self.grid = Gtk.GridView(model=selection, factory=factory,
                                 max_columns=5, min_columns=2,
                                 single_click_activate=True, vexpand=True)
        self.grid.add_css_class("ashy-posters")
        self.grid.connect("activate", self._on_activate)
        self._focus_marquee = FocusMarquee(self.grid)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.grid)
        # GtkWidget has no "width" property to watch; the scrolled window's
        # horizontal adjustment does change with the viewport, so that is
        # what tells us the window was resized.
        sw.get_hadjustment().connect("changed", self._fit_columns)
        sw.set_margin_end(22)

        icon = "ashy-film-symbolic" if media_type is MediaType.MOVIE else "ashy-series-symbolic"
        self.empty = Adw.StatusPage(icon_name=icon, title=_("Nothing found"),
                                    description=_("Add a playlist in “Playlists” or "
                                                  "change the filter."))
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(sw, "grid")
        self.stack.add_named(self.empty, "empty")
        content.append(self.stack)

    # ---- API -----------------------------------------------------------

    def focus_content(self) -> bool:
        """Entering the page from the sidebar lands on the posters."""
        if self.filter_model.get_n_items() == 0:
            return False
        return self.grid.grab_focus()

    def favorite_changed(self, channel: Channel) -> None:
        """A favorite changed elsewhere: repaint the cards on screen only
        (a GridView doesn't rebind a row whose item is the same object)."""
        update_favorite(self.store, channel)
        for list_item in list(self._bound):
            if list_item.get_item() is not None:
                self._on_bind(None, list_item)

    def invalidate(self) -> None:
        self._dirty = True
        if self.get_mapped():
            self.refresh()

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._build_groups()

    def open_chip(self, key: str) -> None:
        """Show one chip's titles (a group, "new" or "top"), e.g. from Home."""
        self._chip = key
        self._dirty = True
        if self.get_mapped():
            self.refresh()
        self._select_group(key)
        if self.filter_model.get_n_items():
            self.grid.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    def set_search(self, text: str) -> None:
        self.search.set_text(text)
        query = people_query(text)
        if query == self._people_query:
            return
        self._people_query = query
        if self._people_source:
            GLib.source_remove(self._people_source)
            self._people_source = 0
        if not query or self.people_search is None:
            self.people.set_cast([])
            return
        self._people_source = GLib.timeout_add(_PEOPLE_DELAY_MS, self._ask_people)

    def _ask_people(self) -> bool:
        self._people_source = 0
        if self.people_search is not None and self._people_query:
            self.people_search(self._people_query, self._on_people)
        return GLib.SOURCE_REMOVE

    def _on_people(self, query: str, people: list) -> None:
        if query == self._people_query:  # an older answer is dropped
            self.people.set_cast(people)

    # ---- data ---------------------------------------------------------

    def update_hidden_count(self) -> None:
        """The groups and their 'Hidden (N)' entry after hiding/showing."""
        if self.get_realized():
            self._build_groups()

    def show_groups(self) -> Gtk.Widget:
        return self.groups.show()

    def _build_groups(self) -> None:
        mt = self.media_type
        groups = self.storage.groups(mt)
        entries = [("all", _("All"), self.storage.count(mt)), ("new", _("New Releases"), None),
                   ("top", _("Trending"), None)]
        entries += [(g, chip_label(g), n) for g, n in groups]
        if self._chip not in {k for k, _l, _n in entries}:
            self._chip = "all"
        self._building = True
        self.group_list.remove_all()
        for key, label, count in entries:
            self._add_group_row(key, label, count)
        hidden = self.storage.hidden_count(mt)
        if hidden:
            row = Gtk.ListBoxRow(selectable=False, tooltip_text=_("See hidden channels and groups"))
            row._key = _HIDDEN  # type: ignore[attr-defined]
            row.add_css_class("ashy-hidden-entry")
            box = Gtk.Box(spacing=8)
            box.append(Gtk.Image(icon_name="view-conceal-symbolic"))
            box.append(Gtk.Label(label=_("Hidden ({n})").format(n=hidden), xalign=0,
                                 hexpand=True, ellipsize=3))
            row.set_child(box)
            self.group_list.append(row)
        self._building = False
        self._select_group(self._chip)
        if self._group_source:
            GLib.source_remove(self._group_source)
            self._group_source = 0
        self._load()

    def _add_group_row(self, key: str, label: str, count: int | None) -> None:
        row = Gtk.ListBoxRow()
        row._key = key  # type: ignore[attr-defined]
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=3,
                             tooltip_text=label))
        if count is not None:
            c = Gtk.Label(label=str(count))
            c.add_css_class("ashy-group-count")
            box.append(c)
        row.set_child(box)
        self.group_list.append(row)
        if key not in _SPECIAL:
            # Right click, long press or the Menu key: hide it.
            add_context_menu(row, lambda: popup_menu(row, [
                (_("Hide category"), lambda: self.emit("hide-group", self.media_type, key))]))

    def _select_group(self, key: str) -> None:
        row = self.group_list.get_first_child()
        while row is not None:
            if getattr(row, "_key", None) == key:
                self._building = True
                self.group_list.select_row(row)
                self._building = False
                return
            row = row.get_next_sibling()

    def _on_group_selected(self, _list, row) -> None:
        if row is None or self._building:
            return
        self._chip = row._key  # type: ignore[attr-defined]
        # Not now: the selection is still moving while a key is held down.
        if self._group_source:
            GLib.source_remove(self._group_source)
        self._group_source = GLib.timeout_add(_GROUP_DELAY_MS, self._load_selected)

    def _load_selected(self) -> bool:
        self._group_source = 0
        self._load()
        if self.filter_model.get_n_items():
            self.grid.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
        return GLib.SOURCE_REMOVE

    def _on_group_activated(self, _list, row) -> None:
        if getattr(row, "_key", None) == _HIDDEN:
            self.emit("show-hidden")

    def _page(self, offset: int, limit: int) -> list[Channel]:
        mt = self.media_type
        if self._chip in ("new", "top"):
            if offset >= _SHORTLIST:
                return []
            limit = min(limit, _SHORTLIST - offset)
            if self._chip == "new":
                return self.storage.channels(mt, order="added",
                                             limit=limit, offset=offset)
            return self.storage.channels(mt, order="rating", min_rating=7.0,
                                         limit=limit, offset=offset)
        group = None if self._chip == "all" else self._chip
        return self.storage.channels(mt, group=group, limit=limit, offset=offset)

    def _load(self) -> None:
        # The first page goes in at once — that is everything the window can
        # show — and the rest arrive a page at a time while the main loop is
        # free. Reading ten thousand rows and making ten thousand objects
        # before drawing anything is what made changing group feel stuck.
        self._load_run += 1
        run = self._load_run
        first = self._page(0, _FIRST_PAGE)
        self.store.splice(0, self.store.get_n_items(),
                          [ChannelObject(c) for c in first])
        self._update_empty()
        if len(first) < _FIRST_PAGE:
            return

        def more(offset: int) -> bool:
            # Another group was opened while this one was still arriving.
            if run != self._load_run:
                return False
            part = self._page(offset, _NEXT_PAGE)
            if part:
                self.store.splice(self.store.get_n_items(), 0,
                                  [ChannelObject(c) for c in part])
                self._update_empty()
            if len(part) == _NEXT_PAGE:
                GLib.idle_add(more, offset + _NEXT_PAGE,
                              priority=GLib.PRIORITY_DEFAULT_IDLE)
            return False

        GLib.idle_add(more, _FIRST_PAGE, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _fit_columns(self, *_a) -> None:
        width = self.grid.get_width()
        if width <= 0:
            return
        columns = max(2, min(12, width // _CARD_W))
        if columns != self.grid.get_max_columns():
            self.grid.set_max_columns(columns)

    def _update_empty(self) -> None:
        self.stack.set_visible_child_name(
            "grid" if self.filter_model.get_n_items() else "empty")

    # ---- factory -------------------------------------------------------

    def _on_setup(self, _f, list_item: Gtk.ListItem) -> None:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=6, margin_bottom=10, margin_start=6, margin_end=6)

        frame = Gtk.Overlay()
        frame.set_size_request(_POSTER_W, _POSTER_H)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.add_css_class("ashy-poster-img")
        placeholder = Gtk.Image(pixel_size=40, halign=Gtk.Align.CENTER,
                                valign=Gtk.Align.CENTER)
        placeholder.add_css_class("ashy-thumb-icon")
        frame.set_child(placeholder)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        pic.set_size_request(_POSTER_W, _POSTER_H)
        frame.add_overlay(pic)
        fav = Gtk.Image(icon_name="ashy-heart-filled-symbolic", halign=Gtk.Align.END,
                        valign=Gtk.Align.START, margin_top=6, margin_end=6)
        fav.add_css_class("ashy-badge")
        frame.add_overlay(fav)

        # Long titles scroll while the card is hovered or focused.
        title = MarqueeLabel(width_chars=14)
        title.add_css_class("ashy-poster-title")
        title.attach_hover(card)
        card._marquee = title  # type: ignore[attr-defined]  # see FocusMarquee

        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        year = Gtk.Label(xalign=0, hexpand=True)
        year.add_css_class("ashy-year")
        rating = Gtk.Label(xalign=1)
        rating.add_css_class("ashy-rating")
        meta.append(year)
        meta.append(rating)

        card.append(frame)
        card.append(title)
        card.append(meta)

        # Right click: toggle favorite.
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_context, list_item, card)
        card.add_controller(click)
        list_item.set_child(card)

    def _on_bind(self, _f, list_item: Gtk.ListItem) -> None:
        obj: ChannelObject = list_item.get_item()
        self._bound.add(list_item)
        ch = obj.channel
        card = list_item.get_child()
        frame = card.get_first_child()
        title = frame.get_next_sibling()
        meta = title.get_next_sibling()
        year = meta.get_first_child()
        rating = year.get_next_sibling()
        placeholder = frame.get_child()
        pic = placeholder.get_next_sibling()
        fav = pic.get_next_sibling()

        title.set_text(_clean_title(ch.display_name))
        title.set_tooltip_text(ch.display_name)
        y = ch.year or ((m := _YEAR_RE.search(ch.display_name)) and m.group(1)) or ""
        year.set_text(y)
        rating.set_text(f"★ {ch.rating:.1f}" if ch.rating > 0 else "")
        fav.set_visible(ch.favorite)
        placeholder.set_from_icon_name(
            "ashy-film-symbolic" if ch.media_type is MediaType.MOVIE
            else "ashy-series-symbolic")
        set_picture(pic, ch.logo, _POSTER_H, lambda: list_item.get_item() is obj)

    def _on_context(self, gesture, _n, x, y, list_item, card) -> None:
        obj = list_item.get_item()
        if obj is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        ch: Channel = obj.channel
        btn = Gtk.Button(label=_("Remove from favorites") if ch.favorite
                         else _("Add to favorites"))
        btn.add_css_class("flat")
        popover = Gtk.Popover(child=btn)
        popover.set_parent(card)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)

        def toggle(*_a):
            popover.popdown()
            if not self.storage.set_favorite(ch, not ch.favorite):
                return
            self.emit("favorite-changed", ch)
            frame = card.get_first_child()
            frame.get_child().get_next_sibling().get_next_sibling().set_visible(ch.favorite)

        btn.connect("clicked", toggle)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    def _on_activate(self, _g, position: int) -> None:
        obj = self.filter_model.get_item(position)
        if obj is not None:
            self.emit("play", obj.channel)


def _clean_title(name: str) -> str:
    """'Duna: Parte Dois - 2024' -> 'Duna: Parte Dois'."""
    return re.sub(r"\s*[-(]\s*(?:19|20)\d{2}\)?\s*$", "", name).strip() or name
