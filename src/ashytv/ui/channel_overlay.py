"""Channel list shown over the video while watching (TiviMate style).

A translucent panel on the left: the channel list centred on the one being
watched, each row with its current programme, and the schedule of the
highlighted channel next to it. The video keeps playing behind.
"""

from __future__ import annotations

import time

from gi.repository import Gio, GObject, Gtk

from ..i18n import _
from ..models import Channel
from .channel_object import ChannelObject
from .pages._common import set_logo


def _hhmm(epoch: int) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


class ChannelOverlay(Gtk.Box):
    __gtype_name__ = "AshyChannelOverlay"

    __gsignals__ = {
        # index in the list given to show()
        "play": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self):
        super().__init__(spacing=0, halign=Gtk.Align.START, valign=Gtk.Align.FILL,
                         visible=False)
        self.add_css_class("ashy-channel-overlay")
        self._guide = None

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_size_request(400, -1)
        self.title = Gtk.Label(label=_("Channels"), xalign=0)
        self.title.add_css_class("ashy-section-title")
        hint = Gtk.Label(label=_("Enter: watch  •  Esc: close"), xalign=0)
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

    # ---- API -----------------------------------------------------------

    def show_channels(self, channels: list[Channel], current: int, guide) -> None:
        self._guide = guide
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
        self.set_visible(False)

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

    def _show_detail(self, ch: Channel) -> None:
        while (child := self.detail.get_first_child()) is not None:
            self.detail.remove(child)
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
