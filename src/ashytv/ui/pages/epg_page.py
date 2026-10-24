"""Program Guide (EPG) page — TV-style grid per channel."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ...i18n import _
from ...models import MediaType
from ...services.storage import collapse_variants
from ..channel_object import ChannelObject
from ..timeline import ROW_HEIGHT, TimeAxis, TimelineRow
from ._common import SearchFilter, page_header, set_logo

_SPAN = 3 * 3600          # visible window: 3 hours
_STEP = 3600              # step of the ◀ ▶ buttons
_CHAN_W = 220             # channel column width


def _day_label(d: date) -> str:
    today = date.today()
    dt = GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0)
    if d == today:
        return _("Today, {date}").format(date=dt.format("%-d %b"))
    if d == today + timedelta(days=1):
        return _("Tomorrow, {date}").format(date=dt.format("%-d %b"))
    if d == today - timedelta(days=1):
        return _("Yesterday, {date}").format(date=dt.format("%-d %b"))
    return dt.format("%a, %-d %b")


def _now_window_start() -> float:
    now = time.time()
    return (int(now) // 1800) * 1800 - 1800  # half an hour before the current block


class EpgPage(Gtk.Box):
    __gtype_name__ = "AshyEpgPage"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self._guide = None
        self._dirty = True
        self._t0 = _now_window_start()
        self._user_moved = False  # True after ◀ ▶ or picking another day
        self._days: list[date] = []

        self.append(page_header(_("Program Guide"),
                                _("See what's on now and what's next")))

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.add_css_class("ashy-panel")
        panel.set_overflow(Gtk.Overflow.HIDDEN)
        panel.set_margin_start(28)
        panel.set_margin_end(28)
        panel.set_margin_bottom(22)
        panel.set_vexpand(True)

        # ---- toolbar: day + navigation ----
        tools = Gtk.Box(spacing=6, margin_top=8, margin_start=8, margin_end=8)
        self.day_dd = Gtk.DropDown(model=Gtk.StringList())
        self.day_dd.add_css_class("flat")
        self.day_dd.connect("notify::selected", self._on_day)
        tools.append(self.day_dd)
        spacer = Gtk.Box(hexpand=True)
        tools.append(spacer)
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("1 hour earlier"))
        prev.add_css_class("flat")
        prev.connect("clicked", lambda *_: self._shift(-_STEP))
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text=_("1 hour later"))
        nxt.add_css_class("flat")
        nxt.connect("clicked", lambda *_: self._shift(_STEP))
        now_btn = Gtk.Button(label=_("Now"))
        now_btn.add_css_class("flat")
        now_btn.connect("clicked", lambda *_: self._go_now())
        tools.append(prev)
        tools.append(now_btn)
        tools.append(nxt)
        panel.append(tools)

        # ---- time axis (same column width as the rows) ----
        head = Gtk.Box(margin_bottom=4)
        corner = Gtk.Box()
        corner.set_size_request(_CHAN_W, -1)
        # Axis corner and every row's channel column share the same width.
        self._chan_width = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._chan_width.add_widget(corner)
        head.append(corner)
        self.axis = TimeAxis()
        self.axis.set_hexpand(True)
        self.axis.set_margin_end(12)
        head.append(self.axis)
        panel.append(head)
        panel.append(Gtk.Separator())

        # ---- rows (virtualized) ----
        self.store = Gio.ListStore.new(ChannelObject)
        self.search = SearchFilter(self._update_empty)
        self.filter_model = Gtk.FilterListModel(model=self.store,
                                                filter=self.search.filter)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", self._on_bind)
        factory.connect("unbind", lambda _f, li: self._bound.discard(li))
        self._bound: set[Gtk.ListItem] = set()
        self.listview = Gtk.ListView(model=Gtk.NoSelection(model=self.filter_model),
                                     factory=factory)
        self.listview.add_css_class("ashy-epg-list")
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(self.listview)

        self.empty = Adw.StatusPage(
            icon_name="x-office-calendar-symbolic", title=_("Guide unavailable"),
            description=_("The guide appears here once your provider's EPG loads."))
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(sw, "list")
        self.stack.add_named(self.empty, "empty")
        panel.append(self.stack)
        self.append(panel)

        GLib.timeout_add_seconds(30, self._tick)

    # ---- API -----------------------------------------------------------

    def set_guide(self, guide) -> None:
        self._guide = guide
        self.invalidate()

    def invalidate(self) -> None:
        self._dirty = True
        if self.get_mapped():
            self.refresh()

    def set_search(self, text: str) -> None:
        self.search.set_text(text)

    def refresh(self) -> None:
        if not self._user_moved and self._guide and \
                self._t0 != _now_window_start():
            # Opened hours later: show what is on now, not the old window.
            self._t0 = _now_window_start()
            if not self._dirty:
                self._select_day_of(self._t0)
                self._apply_window()
        if not self._dirty:
            return
        self._dirty = False
        guide = self._guide
        if not guide:
            self.store.remove_all()
            self._update_empty()
            return
        live = self.storage.channels(MediaType.LIVE)
        if self.storage.group_variants:
            live = collapse_variants(live)
        chans = [c for c in live
                 if c.tvg_id and guide.has_channel(c.tvg_id)]
        self.store.splice(0, self.store.get_n_items(), [ChannelObject(c) for c in chans])
        self._build_days()
        self._apply_window()
        self._update_empty()

    def _update_empty(self) -> None:
        self.stack.set_visible_child_name(
            "list" if self.filter_model.get_n_items() else "empty")

    # ---- time window ----------------------------------------------

    def _build_days(self) -> None:
        start, end = self._guide.day_range()
        today = date.today()
        first = min(today, date.fromtimestamp(start)) if start else today
        last = max(today, date.fromtimestamp(end)) if end else today
        self._days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        self._block_day = True
        self.day_dd.set_model(Gtk.StringList.new([_day_label(d) for d in self._days]))
        self._select_day_of(self._t0)
        self._block_day = False

    def _select_day_of(self, t: float) -> None:
        d = date.fromtimestamp(t + _SPAN / 2)
        if d in self._days:
            self._block_day = True
            self.day_dd.set_selected(self._days.index(d))
            self._block_day = False

    def _on_day(self, dd, _pspec) -> None:
        if getattr(self, "_block_day", False):
            return
        idx = dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._days):
            return
        d = self._days[idx]
        if d == date.today():
            self._t0 = _now_window_start()
        else:
            self._t0 = datetime(d.year, d.month, d.day, 18).timestamp()  # prime time
            self._user_moved = True
        self._apply_window()

    def _shift(self, delta: int) -> None:
        self._user_moved = True
        self._t0 += delta
        self._select_day_of(self._t0)
        self._apply_window()

    def _go_now(self) -> None:
        self._user_moved = False
        self._t0 = _now_window_start()
        self._select_day_of(self._t0)
        self._apply_window()

    def _apply_window(self) -> None:
        self.axis.set_window(self._t0, self._t0 + _SPAN)
        for list_item in list(self._bound):
            if list_item.get_item() is not None:
                self._on_bind(None, list_item)

    def _tick(self) -> bool:
        if self.get_mapped():
            self.axis.queue_draw()
            for list_item in self._bound:
                timeline = list_item.get_child().get_last_child()
                timeline.queue_draw()
        return GLib.SOURCE_CONTINUE

    # ---- factory -------------------------------------------------------

    def _on_setup(self, _f, list_item: Gtk.ListItem) -> None:
        row = Gtk.Box(spacing=0)
        row.set_size_request(-1, ROW_HEIGHT)
        chan = Gtk.Box(spacing=10)
        chan.set_size_request(_CHAN_W, -1)
        num = Gtk.Label(xalign=0.5, margin_start=6)
        num.add_css_class("ashy-chan-num")
        logo = Gtk.Image(pixel_size=26, valign=Gtk.Align.CENTER)
        logo.add_css_class("ashy-logo-badge")
        # The name expands within the column; the column (explicit hexpand=False)
        # keeps a fixed width, aligned with the time axis.
        name = Gtk.Label(xalign=0, ellipsize=3, hexpand=True, margin_end=8,
                         width_chars=18, max_width_chars=18)
        name.add_css_class("ashy-chan-name")
        chan.append(num)
        chan.append(logo)
        chan.append(name)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self._play_item(list_item))
        chan.add_controller(click)
        chan.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        chan.set_hexpand(False)
        self._chan_width.add_widget(chan)
        row.append(chan)
        timeline = TimelineRow()
        timeline.set_hexpand(True)
        timeline.set_margin_end(12)
        row.append(timeline)
        list_item.set_child(row)

    def _play_item(self, list_item) -> None:
        obj = list_item.get_item()
        if obj is not None:
            self.emit("play", obj.channel)

    def _on_bind(self, _f, list_item: Gtk.ListItem) -> None:
        obj: ChannelObject = list_item.get_item()
        self._bound.add(list_item)
        ch = obj.channel
        row = list_item.get_child()
        chan = row.get_first_child()
        num = chan.get_first_child()
        logo = num.get_next_sibling()
        name = logo.get_next_sibling()
        timeline = row.get_last_child()

        num.set_text(str(list_item.get_position() + 1))
        name.set_text(ch.display_name)
        set_logo(logo, ch.logo, 26, "tv-symbolic", lambda: list_item.get_item() is obj)

        t0, t1 = self._t0, self._t0 + _SPAN
        timeline.clear()
        timeline.set_window(t0, t1)
        now = time.time()
        for prog in self._guide.between(ch.tvg_id, t0, t1) if self._guide else []:
            btn = Gtk.Button()
            btn.add_css_class("ashy-epg-cell")
            if prog.start <= now < prog.stop:
                btn.add_css_class("now")
            elif prog.stop <= now:
                btn.add_css_class("past")
            lbl = Gtk.Label(label=prog.title or "—", xalign=0, ellipsize=3)
            btn.set_child(lbl)
            span = (f"{time.strftime('%H:%M', time.localtime(prog.start))} – "
                    f"{time.strftime('%H:%M', time.localtime(prog.stop))}")
            btn.set_tooltip_text(f"{prog.title}\n{span}" +
                                 (f"\n\n{prog.desc[:300]}" if prog.desc else ""))
            btn.connect("clicked", self._on_cell, ch, prog)
            timeline.add_item(btn, prog.start, prog.stop)

    def _on_cell(self, button, channel, prog) -> None:
        if prog.start <= time.time():
            self.emit("play", channel)
            return
        # Future program: show details.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        t = Gtk.Label(label=prog.title, xalign=0, wrap=True, max_width_chars=40)
        t.add_css_class("ashy-chan-name")
        h = Gtk.Label(label=f"{channel.display_name} • "
                            f"{GLib.DateTime.new_from_unix_local(int(prog.start)).format('%x %R')}"
                            f" – {time.strftime('%H:%M', time.localtime(prog.stop))}",
                      xalign=0)
        h.add_css_class("ashy-faint")
        box.append(t)
        box.append(h)
        if prog.desc:
            d = Gtk.Label(label=prog.desc, xalign=0, wrap=True, max_width_chars=50)
            box.append(d)
        pop = Gtk.Popover(child=box)
        pop.set_parent(button)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()
