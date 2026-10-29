"""Catalog page (Movies/Series) — poster grid."""

from __future__ import annotations

import re

from gi.repository import Adw, Gdk, Gio, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ..channel_object import ChannelObject
from ._common import ChipRow, SearchFilter, page_header, set_picture, update_favorite

_POSTER_W = 150
_POSTER_H = 225
# "FILMES - AÇÃO" -> "Ação"; "SERIES - DORAMAS" -> "Doramas"
_PREFIX_RE = re.compile(r"^\s*(filmes?|movies?|vod|s[ée]ries?)\s*[-|:/]\s*", re.I)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def chip_label(group: str) -> str:
    label = _PREFIX_RE.sub("", group).strip() or group
    return label if not label.isupper() else label.title()


class CatalogPage(Gtk.Box):
    __gtype_name__ = "AshyCatalogPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "favorite-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, storage, media_type: MediaType, title: str, subtitle: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self.media_type = media_type
        self._dirty = True
        self._chip = "all"

        self.append(page_header(title, subtitle))
        self.chips = ChipRow(self._on_chip)
        self.append(self.chips)

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

        self.grid = Gtk.GridView(model=selection, factory=factory,
                                 max_columns=12, min_columns=2,
                                 single_click_activate=True, vexpand=True)
        self.grid.add_css_class("ashy-posters")
        self.grid.connect("activate", self._on_activate)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.grid)
        sw.set_margin_start(22)
        sw.set_margin_end(22)

        icon = "ashy-film-symbolic" if media_type is MediaType.MOVIE else "ashy-series-symbolic"
        self.empty = Adw.StatusPage(icon_name=icon, title=_("Nothing found"),
                                    description=_("Add a playlist in “Playlists” or "
                                                  "change the filter."))
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(sw, "grid")
        self.stack.add_named(self.empty, "empty")
        self.append(self.stack)

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
        groups = self.storage.groups(self.media_type)
        chips = [("all", _("All")), ("new", _("New Releases")), ("top", _("Trending"))]
        chips += [(g, chip_label(g)) for g, _n in groups]
        if self._chip not in {k for k, _l in chips}:
            self._chip = "all"
        self.chips.set_chips(chips, self._chip)
        self._load()

    def set_search(self, text: str) -> None:
        self.search.set_text(text)

    # ---- data ---------------------------------------------------------

    def _on_chip(self, key: str) -> None:
        self._chip = key
        self._load()
        if self.filter_model.get_n_items():
            self.grid.scroll_to(0, Gtk.ListScrollFlags.NONE, None)

    def _load(self) -> None:
        mt = self.media_type
        if self._chip == "all":
            chans = self.storage.channels(mt)
        elif self._chip == "new":
            chans = self.storage.channels(mt, order="added", limit=300)
        elif self._chip == "top":
            chans = self.storage.channels(mt, order="rating", min_rating=7.0, limit=300)
        else:
            chans = self.storage.channels(mt, group=self._chip)
        self.store.splice(0, self.store.get_n_items(), [ChannelObject(c) for c in chans])
        self._update_empty()

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

        title = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=14, width_chars=14)
        title.add_css_class("ashy-poster-title")

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
