"""Playlists → servers of one playlist: status of each one, backup addresses."""

from __future__ import annotations

from gi.repository import Adw, GLib, GObject, Gtk

from ..i18n import _
from ..services import failover


def status_text(health) -> str:
    """'Online · 120 ms' / 'Slow · 2300 ms' / 'Down'."""
    if health.status == failover.ONLINE:
        return _("Online · {ms} ms").format(ms=health.latency_ms)
    if health.status == failover.SLOW:
        return _("Slow · {ms} ms").format(ms=health.latency_ms)
    if health.status == failover.DOWN:
        return _("Down")
    return _("not checked yet")


def _when(ts: float) -> str:
    return GLib.DateTime.new_from_unix_local(int(ts)).format("%x %R")


class ServersDialog(Adw.Dialog):
    __gtype_name__ = "AshyServersDialog"

    __gsignals__ = {
        "save": (GObject.SignalFlags.RUN_FIRST, None, (object,)),   # backup URLs
        "check": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, storage, playlist_id: int, name: str, servers: list[str],
                 active: int):
        super().__init__(title=_("Servers of “{name}”").format(name=name),
                         content_width=600, content_height=600)
        self.storage = storage
        self.add_css_class("ashy-dialog")
        self.playlist_id = playlist_id
        self.servers = servers
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        check = Gtk.Button(label=_("Check now"))
        check.connect("clicked", self._on_check)
        header.pack_start(check)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()

        self.status_group = Adw.PreferencesGroup(
            title=_("Status"),
            description=_("Checked when the playlist is updated, every 15 minutes while "
                          "AshyTV is open, and when playback fails."))
        page.add(self.status_group)
        self._rows: list[Adw.ActionRow] = []
        self._fill_status(active)

        edit = Adw.PreferencesGroup(
            title=_("Backup servers (alternative DNS)"),
            description=_("Other addresses of the same provider, with the same login. "
                          "When the main one doesn't answer, AshyTV switches to the "
                          "next automatically."))
        self.entries: list[Adw.EntryRow] = []
        for i in range(failover.MAX_BACKUPS):
            entry = Adw.EntryRow(title=_("Backup server {n}").format(n=i + 1),
                                 input_purpose=Gtk.InputPurpose.URL)
            if i + 1 < len(servers):
                entry.set_text(servers[i + 1])
            edit.add(entry)
            self.entries.append(entry)
        save = Gtk.Button(label=_("Save"), halign=Gtk.Align.END, margin_top=12)
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        edit.add(save)
        page.add(edit)
        view.set_content(page)
        self.set_child(view)

    def _fill_status(self, active: int) -> None:
        for row in self._rows:
            self.status_group.remove(row)
        self._rows = []
        health = failover.health_list(self.storage.server_health(self.playlist_id),
                                      self.servers)
        for i, h in enumerate(health):
            role = _("Main server") if i == 0 else _("Backup server {n}").format(n=i)
            parts = [role, status_text(h)]
            if h.checked:
                parts.append(_("checked {when}").format(when=_when(h.checked)))
            if i == active:
                parts.append(_("in use"))
            row = Adw.ActionRow(title=GLib.markup_escape_text(h.url),
                                subtitle=" · ".join(parts), subtitle_lines=2)
            dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
            dot.add_css_class("ashy-server-dot")
            dot.add_css_class(h.status or "unknown")
            row.add_prefix(dot)
            self.status_group.add(row)
            self._rows.append(row)

    def refresh(self) -> None:
        self._fill_status(self.storage.active_server(self.playlist_id))

    def _on_check(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        self.emit("check")
        # The check runs in the background: show its result a moment later.
        GLib.timeout_add_seconds(int(failover.CHECK_TIMEOUT) + 2, self._after_check, button)

    def _after_check(self, button: Gtk.Button) -> bool:
        button.set_sensitive(True)
        self.refresh()
        return GLib.SOURCE_REMOVE

    def _on_save(self, _b) -> None:
        self.emit("save", [e.get_text().strip() for e in self.entries])
        self.close()
