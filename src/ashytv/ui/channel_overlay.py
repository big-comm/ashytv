"""Channel list shown over the video while watching (TiviMate style).

A translucent panel on the left: the channel list centred on the one being
watched, each row with its current programme, and the schedule of the
highlighted channel next to it. The video keeps playing behind.
"""

from __future__ import annotations

import time

from gi.repository import Gio, GLib, GObject, Gtk

from ..i18n import _
from ..models import Channel
from .channel_object import ChannelObject
from .pages._common import set_logo


# Moving through the groups waits this long before loading a group's channels.
GROUP_DELAY_MS = 250


class GroupObject(GObject.Object):
    """A row of the groups column: key, label and channel count."""

    def __init__(self, key: str, label: str, count: int):
        super().__init__()
        self.key, self.label, self.count = key, label, count


def _hhmm(epoch: int) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


class ChannelOverlay(Gtk.Box):
    __gtype_name__ = "AshyChannelOverlay"

    __gsignals__ = {
        # index in the list given to show()
        "play": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # a group was chosen in the groups column (its key)
        "group-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self):
        super().__init__(spacing=0, halign=Gtk.Align.START, valign=Gtk.Align.FILL,
                         visible=False)
        self.add_css_class("ashy-channel-overlay")
        self._guide = None
        self._group_source = 0
        self._group_key = ""

        # Groups column (← from the channel list): Favorites, Recent, groups.
        self.groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                  visible=False, margin_end=14)
        self.groups_box.set_size_request(260, -1)
        groups_title = Gtk.Label(label=_("Groups"), xalign=0)
        groups_title.add_css_class("ashy-section-title")
        groups_hint = Gtk.Label(label=_("→ or Enter: channels  •  Esc: back"), xalign=0)
        groups_hint.add_css_class("ashy-faint")
        self.groups_store = Gio.ListStore.new(GroupObject)
        self.groups_sel = Gtk.SingleSelection(model=self.groups_store, autoselect=False)
        self.groups_sel.connect("notify::selected", self._on_group_moved)
        gfactory = Gtk.SignalListItemFactory()
        gfactory.connect("setup", self._on_group_setup)
        gfactory.connect("bind", self._on_group_bind)
        self.groups_view = Gtk.ListView(model=self.groups_sel, factory=gfactory,
                                        single_click_activate=True, vexpand=True)
        self.groups_view.add_css_class("ashy-chanlist")
        self.groups_view.add_css_class("ashy-overlay-groups")
        self.groups_view.connect("activate", lambda _v, pos: self.open_group(pos))
        gsw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        gsw.set_child(self.groups_view)
        for w in (groups_title, groups_hint, gsw):
            self.groups_box.append(w)
        self.append(self.groups_box)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(400, -1)
        self.title = Gtk.Label(label=_("Channels"), xalign=0)
        self.title.add_css_class("ashy-section-title")
        hint = Gtk.Label(label=_("Enter: watch  •  ←: groups  •  Esc: close"), xalign=0)
        hint.add_css_class("ashy-faint")
        left.append(self.title)
        left.append(hint)

        self.store = Gio.ListStore.new(ChannelObject)
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=True)
        self.selection.connect("notify::selected", self._on_selected)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        self.listview = Gtk.ListView(model=self.selection, factory=factory,
                                     single_click_activate=True, vexpand=True)
        self.listview.add_css_class("ashy-chanlist")
        self.listview.connect("activate", lambda _lv, pos: self.emit("play", pos))
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.listview)
        left.append(sw)
        self.append(left)

        # Schedule of the highlighted channel.
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.detail.add_css_class("ashy-overlay-detail")
        self.detail.set_size_request(320, -1)
        self.append(self.detail)

        # The selection follows the keyboard/remote focus in both lists (the
        # remote moves the focus, not the selection), so the channels and the
        # schedule follow what is highlighted.
        self._focus_handler = 0
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    def _on_map(self, _w) -> None:
        root = self.get_root()
        if root is not None and not self._focus_handler:
            self._focus_handler = root.connect("notify::focus-widget", self._on_focus)

    def _on_unmap(self, _w) -> None:
        root = self.get_root()
        if root is not None and self._focus_handler:
            root.disconnect(self._focus_handler)
        self._focus_handler = 0

    def _on_focus(self, root, _p) -> None:
        focus = root.get_focus()
        if focus is None:
            return
        for view, selection in ((self.groups_view, self.groups_sel),
                                (self.listview, self.selection)):
            if focus.get_parent() is view:
                item = getattr(focus.get_first_child(), "_list_item", None)
                pos = item.get_position() if item is not None else Gtk.INVALID_LIST_POSITION
                if pos != Gtk.INVALID_LIST_POSITION and pos != selection.get_selected():
                    selection.set_selected(pos)

    # ---- API -----------------------------------------------------------

    def show_channels(self, channels: list[Channel], current: int, guide,
                      title: str = "") -> None:
        self._guide = guide
        self.title.set_text(title or _("Channels"))
        self.groups_box.set_visible(False)
        self.store.splice(0, self.store.get_n_items(),
                          [ChannelObject(c) for c in channels])
        self.set_visible(True)
        if channels:
            current = max(0, min(current, len(channels) - 1))
            self.selection.set_selected(current)
            self.listview.scroll_to(current, Gtk.ListScrollFlags.FOCUS
                                    | Gtk.ListScrollFlags.SELECT, None)
            self._show_detail(channels[current])
        self.listview.grab_focus()

    def hide(self) -> None:
        self._cancel_group()
        self.groups_box.set_visible(False)
        self.set_visible(False)

    # ---- groups column -------------------------------------------------

    @property
    def groups_visible(self) -> bool:
        return self.groups_box.get_visible()

    @property
    def groups_focused(self) -> bool:
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        return focus is not None and focus.is_ancestor(self.groups_view)

    def show_groups(self, groups: list[tuple[str, str, int]], current: str) -> None:
        """groups = [(key, label, count)]; `current` gets the focus."""
        self._cancel_group()
        self._group_key = current
        self.groups_store.splice(0, self.groups_store.get_n_items(),
                                 [GroupObject(k, label, n) for k, label, n in groups])
        index = next((i for i, (k, _l, _n) in enumerate(groups) if k == current), 0)
        self.groups_box.set_visible(True)
        if groups:
            # Selecting it must not reload the channels shown: same group.
            self.groups_sel.set_selected(index)
            self.groups_view.scroll_to(index, Gtk.ListScrollFlags.FOCUS, None)
        self.groups_view.grab_focus()

    def hide_groups(self) -> None:
        self._cancel_group()
        self.groups_box.set_visible(False)
        self.focus_channels()

    def focus_groups(self) -> None:
        pos = self.groups_sel.get_selected()
        if pos != Gtk.INVALID_LIST_POSITION:
            self.groups_view.scroll_to(pos, Gtk.ListScrollFlags.FOCUS, None)
        self.groups_view.grab_focus()

    def focus_channels(self) -> None:
        pos = self.selection.get_selected()
        if pos != Gtk.INVALID_LIST_POSITION:
            self.listview.scroll_to(pos, Gtk.ListScrollFlags.FOCUS, None)
        self.listview.grab_focus()

    def set_channels(self, channels: list[Channel], title: str) -> None:
        """Another group's channels in the channel column (focus stays)."""
        self.title.set_text(title)
        self.store.splice(0, self.store.get_n_items(), [ChannelObject(c) for c in channels])
        if channels:
            self.selection.set_selected(0)
            self.listview.scroll_to(0, Gtk.ListScrollFlags.NONE, None)
            self._show_detail(channels[0])
        else:
            self._clear_detail()

    def _on_group_setup(self, _f, item: Gtk.ListItem) -> None:
        row = Gtk.Box(spacing=10)
        name = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        name.add_css_class("ashy-chan-name")
        count = Gtk.Label(xalign=1)
        count.add_css_class("ashy-group-count")
        row.append(name)
        row.append(count)
        row._list_item = item  # type: ignore[attr-defined]  # focus -> row
        item.set_child(row)

    def _on_group_bind(self, _f, item: Gtk.ListItem) -> None:
        obj = item.get_item()
        name = item.get_child().get_first_child()
        name.set_text(obj.label)
        name.get_next_sibling().set_text(str(obj.count) if obj.count >= 0 else "")

    def _on_group_moved(self, *_a) -> None:
        obj = self.groups_sel.get_selected_item()
        if obj is None or obj.key == self._group_key:
            return
        self._cancel_group()
        self._group_source = GLib.timeout_add(GROUP_DELAY_MS, self._emit_group, obj.key)

    def _emit_group(self, key: str) -> bool:
        self._group_source = 0
        self._group_key = key
        self.emit("group-selected", key)
        return GLib.SOURCE_REMOVE

    def open_group(self, position: int) -> None:
        """Click, Enter or → on a group: its channels, now."""
        obj = self.groups_store.get_item(position)
        if obj is None:
            return
        self._cancel_group()
        if obj.key != self._group_key:
            self._emit_group(obj.key)
        self.focus_channels()

    def _cancel_group(self) -> None:
        if self._group_source:
            GLib.source_remove(self._group_source)
            self._group_source = 0

    @property
    def selected(self) -> int:
        return self.selection.get_selected()

    # ---- rows ----------------------------------------------------------

    def _on_setup(self, _f, item: Gtk.ListItem) -> None:
        row = Gtk.Box(spacing=10)
        num = Gtk.Label(width_chars=4, xalign=1)
        num.add_css_class("ashy-chan-num")
        logo = Gtk.Image(pixel_size=28)
        logo.add_css_class("ashy-logo-badge")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        name = Gtk.Label(xalign=0, ellipsize=3)
        name.add_css_class("ashy-chan-name")
        prog = Gtk.Label(xalign=0, ellipsize=3)
        prog.add_css_class("ashy-chan-prog")
        bar = Gtk.ProgressBar()
        bar.add_css_class("ashy-progress-mini")
        for w in (name, prog, bar):
            texts.append(w)
        for w in (num, logo, texts):
            row.append(w)
        row._list_item = item  # type: ignore[attr-defined]  # focus -> row
        # The number is the row's position: it changes when a search filters
        # the list without the row being bound again.
        item.connect("notify::position",
                     lambda li, _p: num.set_text(str(li.get_position() + 1)))
        item.set_child(row)

    def _on_bind(self, _f, item: Gtk.ListItem) -> None:
        obj = item.get_item()
        ch = obj.channel
        row = item.get_child()
        num = row.get_first_child()
        logo = num.get_next_sibling()
        texts = logo.get_next_sibling()
        name = texts.get_first_child()
        prog = name.get_next_sibling()
        bar = prog.get_next_sibling()
        num.set_text(str(item.get_position() + 1))
        name.set_text(ch.base_name if len(ch.variant_labels) > 1 else ch.display_name)
        set_logo(logo, ch.logo, 28, "tv-symbolic", lambda: item.get_item() is obj)
        cur = self._guide.now_next(ch.tvg_id)[0] if self._guide and ch.tvg_id else None
        prog.set_text(cur.title if cur else "")
        prog.set_visible(cur is not None)
        bar.set_visible(cur is not None)
        if cur:
            bar.set_fraction(cur.progress(int(time.time())))

    # ---- schedule ------------------------------------------------------

    def _on_selected(self, *_a) -> None:
        obj = self.selection.get_selected_item()
        if obj is not None:
            self._show_detail(obj.channel)

    def _clear_detail(self) -> None:
        while (child := self.detail.get_first_child()) is not None:
            self.detail.remove(child)

    def _show_detail(self, ch: Channel) -> None:
        self._clear_detail()
        title = Gtk.Label(label=ch.base_name if len(ch.variant_labels) > 1
                          else ch.display_name, xalign=0, ellipsize=3)
        title.add_css_class("ashy-section-title")
        self.detail.append(title)
        now = int(time.time())
        progs = (self._guide.between(ch.tvg_id, now, now + 6 * 3600)[:5]
                 if self._guide and ch.tvg_id else [])
        if not progs:
            empty = Gtk.Label(label=_("No schedule"), xalign=0)
            empty.add_css_class("ashy-faint")
            self.detail.append(empty)
            return
        for i, p in enumerate(progs):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("ashy-overlay-prog")
            if i == 0 and p.start <= now:
                box.add_css_class("now")
            when = Gtk.Label(label=f"{_hhmm(p.start)} – {_hhmm(p.stop)}", xalign=0)
            when.add_css_class("ashy-faint")
            name = Gtk.Label(label=p.title or "—", xalign=0, wrap=True)
            name.add_css_class("ashy-chan-name")
            box.append(when)
            box.append(name)
            if i == 0 and p.start <= now:
                bar = Gtk.ProgressBar(fraction=p.progress(now))
                bar.add_css_class("ashy-progress-mini")
                box.append(bar)
                if p.desc:
                    desc = Gtk.Label(label=p.desc, xalign=0, wrap=True, lines=3,
                                     ellipsize=3)
                    desc.add_css_class("ashy-faint")
                    box.append(desc)
            self.detail.append(box)
