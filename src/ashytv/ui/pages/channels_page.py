"""Channels page — category chips + groups + channel list."""

from __future__ import annotations

import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ...i18n import _
from ...models import Channel, MediaType
from ...services.text import normalize
from ...services.storage import collapse_variants
from ..channel_object import ChannelObject
from .epg_page import EpgPage
from ._common import ChipRow, SearchFilter, page_header, set_logo, update_favorite

_ALL = "__all__"

# Shortcut chips: (key, label, generic terms matched against the group name).
_KEYWORD_CHIPS = [
    ("sports", _("Sports"), ("esport", "sport", "futebol", "football", "soccer",
                            "jogos", "combate", "fight", "racing", "corrida")),
    ("news", _("News"), ("notic", "news", "jornal")),
    ("open", _("Free-to-air"), ("abert", "aberta", "free", "local", "regional")),
    ("movies", _("Movies"), ("filme", "movie", "cine")),
    ("series", _("Series"), ("serie", "seriado", "novela", "show")),
    ("kids", _("Kids"), ("infant", "kids", "crianca", "desenho", "cartoon",
                          "anime", "kid")),
    ("docs", _("Documentaries"), ("document", "docs", "natureza", "nature",
                               "ciencia", "science")),
    ("music", _("Music"), ("music", "musica", "radio")),
]


class ChannelsPage(Gtk.Box):
    __gtype_name__ = "AshyChannelsPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "favorite-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "show-schedule": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "hide-group": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "remind": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "move-group": (GObject.SignalFlags.RUN_FIRST, None, (str, int)),
    }

    def __init__(self, storage, media_type: MediaType = MediaType.LIVE,
                 favorites_only: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self.media_type = media_type
        self.favorites_only = favorites_only
        self._guide = None
        self._dirty = True
        self._group_key = _ALL
        self._chip = "all"

        title = _("Favorites") if favorites_only else _("TV Channels")
        sub = (_("Channels, movies and series you marked") if favorites_only
               else _("All your channels organized by category"))
        header = page_header(title, sub)
        header.set_hexpand(True)
        top = Gtk.Box()
        top.append(header)
        self.guide: EpgPage | None = None
        if not favorites_only:
            # List | Guide: the same channels as a list or as a time grid.
            switch = Gtk.Box(valign=Gtk.Align.CENTER, margin_end=28, margin_top=8)
            switch.add_css_class("ashy-tabs")
            self._view_buttons: dict[str, Gtk.ToggleButton] = {}
            first = None
            for key, label, icon in (("list", _("List"), "view-list-symbolic"),
                                     ("guide", _("Guide"), "x-office-calendar-symbolic")):
                content = Gtk.Box(spacing=6)
                content.append(Gtk.Image(icon_name=icon))
                content.append(Gtk.Label(label=label))
                btn = Gtk.ToggleButton(child=content, tooltip_text=label)
                if first is None:
                    first = btn
                else:
                    btn.set_group(first)
                btn.connect("toggled", self._on_view_toggled, key)
                self._view_buttons[key] = btn
                switch.append(btn)
            top.append(switch)
        self.append(top)

        self.chips = ChipRow(self._on_chip)
        if not favorites_only:
            self.append(self.chips)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        body.set_margin_start(28)
        body.set_margin_end(28)
        body.set_margin_bottom(22)
        body.set_vexpand(True)

        # ---- groups column ----
        self.group_list = Gtk.ListBox()
        self.group_list.add_css_class("ashy-grouplist")
        self.group_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.group_list.connect("row-selected", self._on_group_selected)
        gscroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     vexpand=True, hexpand=True)
        gscroll.set_child(self.group_list)
        gpanel = Gtk.Box()
        gpanel.add_css_class("ashy-panel")
        gpanel.set_size_request(250, -1)
        gpanel.set_hexpand(False)  # don't inherit the inner list's hexpand
        gpanel.set_overflow(Gtk.Overflow.HIDDEN)
        gpanel.append(gscroll)
        if not favorites_only:
            body.append(gpanel)

        # ---- channel list (virtualized) ----
        self.store = Gio.ListStore.new(ChannelObject)
        self.search = SearchFilter(self._update_empty)
        self.filter_model = Gtk.FilterListModel(model=self.store,
                                                filter=self.search.filter)
        self.selection = Gtk.SingleSelection(model=self.filter_model,
                                             autoselect=False, can_unselect=True)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", lambda _f, li: self._bound.discard(li))
        self._bound: set[Gtk.ListItem] = set()

        self.listview = Gtk.ListView(model=self.selection, factory=factory,
                                     single_click_activate=True, vexpand=True)
        self.listview.add_css_class("ashy-chanlist")
        self.listview.connect("activate", self._on_activate)
        cscroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        cscroll.set_child(self.listview)

        self.empty = Adw.StatusPage(
            icon_name="ashy-heart-symbolic" if favorites_only else "tv-symbolic",
            title=_("Nothing here yet"),
            description=(_("Tap the heart on a channel, movie or series to "
                           "keep it here.") if favorites_only
                         else _("No channels in this category.")))
        self.empty.add_css_class("compact")
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(cscroll, "list")
        self.list_stack.add_named(self.empty, "empty")

        cpanel = Gtk.Box(hexpand=True)
        cpanel.add_css_class("ashy-panel")
        cpanel.set_overflow(Gtk.Overflow.HIDDEN)
        cpanel.append(self.list_stack)
        self.view_stack = Gtk.Stack(hexpand=True,
                                    transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.add_named(cpanel, "list")
        if not favorites_only:
            self.guide = EpgPage(storage, embedded=True)
            self.guide.connect("play", lambda _g, ch: self.emit("play", ch))
            self.guide.connect("remind", lambda _g, ch, prog: self.emit("remind", ch, prog))
            self.view_stack.add_named(self.guide, "guide")
            self._view_buttons["list"].set_active(True)
        body.append(self.view_stack)
        self.append(body)

        # Refresh "now playing" every minute (visible rows only).
        GLib.timeout_add_seconds(60, self._tick)

    # ---- API used by the window ----------------------------------------

    def invalidate(self) -> None:
        self._dirty = True
        if self.get_mapped():
            self.refresh()

    def refresh(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self.favorites_only:
            self._load_channels(self._collapse(
                self.storage.channels(favorites=True, order="favorite")))
            return
        self._build_chips()
        self._build_groups()

    def set_guide(self, guide) -> None:
        self._guide = guide
        if self.guide is not None:
            self.guide.set_guide(guide)
        self._rebind_visible()

    def set_search(self, text: str) -> None:
        self.search.set_text(text)
        if self.guide is not None:
            self.guide.set_search(text)

    def focus_channel(self, channel: Channel) -> None:
        for i in range(self.filter_model.get_n_items()):
            if self.filter_model.get_item(i).channel.url == channel.url:
                self.selection.set_selected(i)
                self.listview.scroll_to(i, Gtk.ListScrollFlags.FOCUS, None)
                return

    # ---- chips and groups ------------------------------------------------

    def _build_chips(self) -> None:
        groups = [g for g, _n in self.storage.groups(self.media_type)]
        keys = [normalize(g) for g in groups]
        chips = [("all", _("All")), ("fav", _("Favorites")), ("recent", _("Recent"))]
        for key, label, words in _KEYWORD_CHIPS:
            if any(w in k for k in keys for w in words):
                chips.append((key, label))
        if self._chip not in {k for k, _l in chips}:
            self._chip = "all"
        self.chips.set_chips(chips, self._chip)

    def _on_chip(self, key: str) -> None:
        self._chip = key
        self._group_key = _ALL
        self._build_groups()

    def _chip_groups(self) -> list[tuple[str, int]]:
        groups = self.storage.groups(self.media_type, collapse=self._grouped)
        words = next((w for k, _l, w in _KEYWORD_CHIPS if k == self._chip), None)
        if words is None:
            return groups
        return [(g, n) for g, n in groups if any(w in normalize(g) for w in words)]

    def _build_groups(self) -> None:
        self.group_list.remove_all()
        if self._chip == "fav":
            self._add_group_row(_("Favorites"),
                                self.storage.count(self.media_type, favorites=True,
                                                   collapse=self._grouped), _ALL)
        elif self._chip == "recent":
            self._add_group_row(_("Recently watched"),
                                len(self._collapse(self.storage.recent_channels(
                                    self.media_type))), _ALL)
        else:
            groups = self._chip_groups()
            label = (_("All Channels") if self._chip == "all"
                     else _("All in this category"))
            total = (self.storage.count(self.media_type, collapse=self._grouped)
                     if self._chip == "all"
                     else sum(n for _g, n in groups))
            self._add_group_row(label, total, _ALL)
            for name, count in groups:
                self._add_group_row(name, count, name)
        target = 0
        for i, row in enumerate(self._rows()):
            if row._key == self._group_key:  # type: ignore[attr-defined]
                target = i
        self.group_list.select_row(self.group_list.get_row_at_index(target))

    def _rows(self):
        row = self.group_list.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _add_group_row(self, name: str, count: int, key: str) -> None:
        row = Gtk.ListBoxRow()
        row._key = key  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.append(Gtk.Label(label=name, xalign=0, hexpand=True, ellipsize=3))
        c = Gtk.Label(label=str(count))
        c.add_css_class("ashy-group-count")
        box.append(c)
        row.set_child(box)
        self.group_list.append(row)
        if key != _ALL and not self.favorites_only:
            # Right click or the Menu key: hide / reorder this group.
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", lambda *_: self._group_menu(row, key))
            row.add_controller(click)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_group_key, row, key)
            row.add_controller(keys)

    def _on_group_key(self, _c, keyval, _code, _state, row, key) -> bool:
        if keyval == Gdk.KEY_Menu:
            self._group_menu(row, key)
            return True
        return False

    def _group_menu(self, row: Gtk.ListBoxRow, group: str) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover(child=box)
        popover.set_parent(row)

        def item(label, callback):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (popover.popdown(), callback()))
            box.append(b)

        item(_("Move up"), lambda: self.emit("move-group", group, -1))
        item(_("Move down"), lambda: self.emit("move-group", group, 1))
        item(_("Hide group"), lambda: self.emit("hide-group", group))
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    def _on_group_selected(self, _list, row) -> None:
        if row is None:
            return
        key = row._key  # type: ignore[attr-defined]
        self._group_key = key
        if self._chip == "fav":
            chans = self.storage.channels(self.media_type, favorites=True)
        elif self._chip == "recent":
            chans = self.storage.recent_channels(self.media_type)
        elif key != _ALL:
            chans = self.storage.channels(self.media_type, group=key)
        elif self._chip == "all":
            chans = self.storage.channels(self.media_type)
        else:
            groups = [g for g, _n in self._chip_groups()]
            chans = self.storage.channels(self.media_type, groups=groups) if groups else []
        self._load_channels(self._collapse(chans))

    @property
    def _grouped(self) -> bool:
        return self.storage.group_variants

    def _collapse(self, channels: list[Channel]) -> list[Channel]:
        return collapse_variants(channels) if self._grouped else channels

    def _load_channels(self, channels: list[Channel]) -> None:
        self.store.splice(0, self.store.get_n_items(),
                          [ChannelObject(c) for c in channels])
        self._update_empty()
        if self.guide is not None:
            self.guide.set_channels(channels)

    # ---- list / guide view -------------------------------------------

    @property
    def view(self) -> str:
        return self.view_stack.get_visible_child_name()

    def set_view(self, view: str) -> None:
        if self.guide is not None and view in self._view_buttons:
            self._view_buttons[view].set_active(True)

    def _on_view_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if not button.get_active():
            return
        self.view_stack.set_visible_child_name(key)
        if key == "guide" and self.guide is not None:
            self.guide.refresh()

    def focus_content(self) -> bool:
        if self.view == "guide" and self.guide is not None:
            return self.guide.focus_content()
        if self.filter_model.get_n_items() == 0:
            return False  # empty: the caller focuses the first control instead
        return self.listview.grab_focus()

    def _update_empty(self) -> None:
        empty = self.filter_model.get_n_items() == 0
        self.list_stack.set_visible_child_name("empty" if empty else "list")

    def _tick(self) -> bool:
        if self._guide and self.get_mapped():
            self._rebind_visible()
        return GLib.SOURCE_CONTINUE

    def _rebind_visible(self) -> None:
        # Only bound (visible) rows: doesn't touch scroll/selection.
        for list_item in list(self._bound):
            if list_item.get_item() is not None:
                self._on_bind(None, list_item)

    # ---- factory -------------------------------------------------------

    def _on_setup(self, _f, list_item: Gtk.ListItem) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        num = Gtk.Label(xalign=0.5)
        num.add_css_class("ashy-chan-num")

        logo = Gtk.Image(pixel_size=30, valign=Gtk.Align.CENTER)
        logo.add_css_class("ashy-logo-badge")

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                        valign=Gtk.Align.CENTER, spacing=2)
        name_row = Gtk.Box(spacing=8)
        name = Gtk.Label(xalign=0, ellipsize=3)
        name.add_css_class("ashy-chan-name")
        qualities = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        qualities.add_css_class("ashy-quality")
        name_row.append(name)
        name_row.append(qualities)
        prog = Gtk.Label(xalign=0, ellipsize=3)
        prog.add_css_class("ashy-chan-prog")
        bar = Gtk.ProgressBar()
        bar.add_css_class("ashy-progress-mini")
        texts.append(name_row)
        texts.append(prog)
        texts.append(bar)

        fav = Gtk.Button(icon_name="ashy-heart-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text=_("Favorite"))
        fav.add_css_class("flat")
        fav.add_css_class("circular")
        fav.add_css_class("ashy-heart")
        fav.connect("clicked", self._on_fav_clicked, list_item)

        more = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text=_("More options"))
        more.add_css_class("flat")
        more.add_css_class("circular")
        more.set_create_popup_func(self._build_menu, list_item)

        row.append(num)
        row.append(logo)
        row.append(texts)
        row.append(fav)
        row.append(more)
        # The number is the row's position: it changes when a search filters
        # the list without the row being bound again.
        list_item.connect("notify::position",
                          lambda li, _p: num.set_text(str(li.get_position() + 1)))
        list_item.set_child(row)

    def _build_menu(self, button: Gtk.MenuButton, list_item: Gtk.ListItem) -> None:
        obj = list_item.get_item()
        if obj is None:
            return
        ch = obj.channel
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popover = Gtk.Popover(child=box)

        def item(label, callback):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (popover.popdown(), callback()))
            box.append(b)

        item(_("Watch"), lambda: self.emit("play", ch))
        item(_("Remove from favorites") if ch.favorite else _("Add to favorites"),
             lambda: self._toggle_fav(ch))
        if ch.media_type is MediaType.LIVE and ch.tvg_id:
            item(_("View schedule"), lambda: self.emit("show-schedule", ch))
        button.set_popover(popover)

    def _on_fav_clicked(self, _button: Gtk.Button, list_item: Gtk.ListItem) -> None:
        obj = list_item.get_item()
        if obj is not None:
            self._toggle_fav(obj.channel)

    def _on_bind(self, _f, list_item: Gtk.ListItem) -> None:
        obj: ChannelObject = list_item.get_item()
        self._bound.add(list_item)
        ch = obj.channel
        row = list_item.get_child()
        num = row.get_first_child()
        logo = num.get_next_sibling()
        texts = logo.get_next_sibling()
        fav = texts.get_next_sibling()
        name_row = texts.get_first_child()
        name = name_row.get_first_child()
        qualities = name.get_next_sibling()
        prog = name_row.get_next_sibling()
        bar = prog.get_next_sibling()

        num.set_text(str(list_item.get_position() + 1))
        grouped = bool(ch.variant_labels) and bool(ch.base_name)
        name.set_text(ch.base_name if grouped else ch.display_name)
        qualities.set_text(" · ".join(ch.variant_labels) if grouped else "")
        qualities.set_visible(grouped)
        fallback = {MediaType.LIVE: "tv-symbolic", MediaType.MOVIE: "ashy-film-symbolic",
                    MediaType.SERIES: "ashy-series-symbolic"}[ch.media_type]
        set_logo(logo, ch.logo, 30, fallback, lambda: list_item.get_item() is obj)

        prog_txt, frac = "", -1.0
        if ch.media_type is MediaType.LIVE:
            if self._guide and ch.tvg_id:
                cur, _n = self._guide.now_next(ch.tvg_id)
                if cur and cur.title:
                    prog_txt = cur.title
                    frac = cur.progress(int(time.time()))
        else:
            prog_txt = " • ".join(p for p in (ch.media_type.label, ch.year) if p)
        prog.set_text(prog_txt)
        prog.set_visible(bool(prog_txt))
        bar.set_visible(frac >= 0)
        bar.set_fraction(max(0.0, frac))
        self._paint_fav(fav, ch.favorite)

    @staticmethod
    def _paint_fav(button: Gtk.Button, active: bool) -> None:
        button.set_icon_name("ashy-heart-filled-symbolic" if active
                             else "ashy-heart-symbolic")
        if active:
            button.add_css_class("active")
        else:
            button.remove_css_class("active")

    def _toggle_fav(self, channel: Channel) -> None:
        if not self.storage.set_favorite(channel, not channel.favorite):
            return
        self.emit("favorite-changed", channel)
        self.favorite_changed(channel)

    def favorite_changed(self, channel: Channel) -> None:
        """Update the rows of one channel in place: no reload, so the scroll
        position and the keyboard focus stay where they are."""
        if self.favorites_only and not channel.favorite:
            # Removed from Favorites: drop just its rows.
            for i in reversed(range(self.store.get_n_items())):
                ch = self.store.get_item(i).channel
                if ch.playlist_id == channel.playlist_id and ch.url == channel.url:
                    self.store.remove(i)
        elif self.favorites_only:
            self._dirty = True  # a new favorite: its place in the list needs a reload
            if self.get_mapped():
                self.refresh()
        else:
            update_favorite(self.store, channel)
            self._rebind_visible()

    def _on_activate(self, _lv, position: int) -> None:
        obj = self.filter_model.get_item(position)
        if obj is not None:
            self.emit("play", obj.channel)
