"""Dialog with a channel's schedule (next 24 h).

The schedule is also where a programme is set to be recorded or reminded
about. It used to be only in the guide, three levels into the Channels page,
which is far enough in that people reported the feature as missing — and this
dialog is one click from any channel in the list.
"""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..models import Channel


def _flip(btn: Gtk.Button) -> None:
    """Show the opposite state: the window owns the book, this only reflects
    the flip so the row answers at once instead of waiting for a rebuild."""
    if btn.has_css_class("accent"):
        btn.remove_css_class("accent")
    else:
        btn.add_css_class("accent")


def _action(icon: str, label: str, on: bool, clicked) -> Gtk.Button:
    """A round icon button that shows whether the thing is already set."""
    b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=label)
    b.add_css_class("flat")
    b.add_css_class("circular")
    b.update_property([Gtk.AccessibleProperty.LABEL], [label])
    if on:
        b.add_css_class("accent")
    b.connect("clicked", lambda *_: clicked(b))
    return b


def schedule_dialog(channel: Channel, guide, on_play,
                    on_remind=None, on_record=None,
                    reminders=None, recordings=None) -> Adw.Dialog:
    """The channel's next 24 hours.

    `on_remind` and `on_record` take (channel, programme) and toggle; the
    books are read to show what is already set. Pass none of them and the
    dialog is the plain listing it was before.
    """
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
            _add_actions(row, channel, p, now, on_remind, on_record,
                         reminders, recordings)
            group.add(row)
        page.add(group)
        view.set_content(page)
    dialog.set_child(view)
    return dialog


def _add_actions(row, channel, prog, now, on_remind, on_record,
                 reminders, recordings) -> None:
    """Remind and record buttons, for a programme that has not ended yet.

    A programme already over offers neither: there is nothing left to record
    and nothing to be reminded of.
    """
    if prog.stop <= now:
        return
    key = (channel.playlist_id, channel.url, int(prog.start))

    if on_remind is not None:
        set_ = bool(reminders and reminders.has(*key))
        def remind(btn):
            on_remind(channel, prog)
            _flip(btn)
        row.add_suffix(_action("alarm-symbolic",
                               _("Cancel reminder") if set_ else _("Remind me"),
                               set_, remind))

    if on_record is not None and prog.start > now:
        # Recording from the middle of a programme would save a piece of it,
        # which is not what "record this programme" means.
        taped = bool(recordings and recordings.has(*key))
        def record(btn):
            on_record(channel, prog)
            _flip(btn)
        row.add_suffix(_action("ashy-record-symbolic",
                               _("Cancel recording") if taped else _("Record"),
                               taped, record))
