"""Dialog with a channel's schedule (next 24 h)."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..models import Channel


def schedule_dialog(channel: Channel, guide, on_play) -> Adw.Dialog:
    dialog = Adw.Dialog(title=channel.display_name, content_width=520,
                        content_height=560)
    view = Adw.ToolbarView()
    header = Adw.HeaderBar()
    play = Gtk.Button(label=_("Watch"))
    play.add_css_class("suggested-action")
    play.connect("clicked", lambda *_: (dialog.close(), on_play(channel)))
    header.pack_start(play)
    view.add_top_bar(header)

    now = int(time.time())
    progs = guide.between(channel.tvg_id, now, now + 24 * 3600) if guide else []
    if not progs:
        view.set_content(Adw.StatusPage(icon_name="x-office-calendar-symbolic",
                                        title=_("No schedule")))
    else:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        day = ""
        for p in progs:
            d = time.strftime("%d/%m", time.localtime(p.start))
            if d != day:
                if day:
                    page.add(group)
                    group = Adw.PreferencesGroup()
                group.set_title(_("Today") if d == time.strftime("%d/%m") else d)
                day = d
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(p.title or "—"),
                subtitle=GLib.markup_escape_text(p.desc[:200]) if p.desc else "",
                subtitle_lines=2)
            hour = Gtk.Label(label=time.strftime("%H:%M", time.localtime(p.start)))
            hour.add_css_class("ashy-epg-now-label" if p.start <= now < p.stop
                               else "ashy-chan-num")
            row.add_prefix(hour)
            group.add(row)
        page.add(group)
        view.set_content(page)
    dialog.set_child(view)
    return dialog
