"""Playlists → edit one playlist: name, address, login, backups and guide link."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from .. import compat
from ..i18n import _
from ..services import failover, http

# What each kind of playlist lets the user change.
XTREAM, URL, FILE, FREE = "xtream", "url", "file", "free"


class PlaylistDialog(Adw.Dialog):
    """Emits "save" with a dict of the values; the window answers with
    finish(): None closes the dialog, a message shows it as an error."""

    __gtype_name__ = "AshyPlaylistDialog"

    __gsignals__ = {
        "save": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, kind: str, name: str, *, host: str = "", username: str = "",
                 password: str = "", url: str = "", backups: list[str] | None = None,
                 guide: str = "", guide_placeholder: str = ""):
        super().__init__(title=_("Edit playlist"), content_width=560)
        self.kind = kind
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save_btn = Gtk.Button()
        sbox = Gtk.Box(spacing=8)
        self.spinner = compat.spinner(visible=False)
        sbox.append(self.spinner)
        sbox.append(Gtk.Label(label=_("Save")))
        self.save_btn.set_child(sbox)
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._on_save)
        header.pack_end(self.save_btn)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        # What failed, on top: the backups may push the bottom out of sight.
        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class("error")
        self.error_group = Adw.PreferencesGroup(visible=False)
        self.error_group.add(self.error)
        page.add(self.error_group)
        main = Adw.PreferencesGroup()
        self.name_row = self._entry(main, _("Playlist name"), name)
        self.host_row = self.user_row = self.pass_row = self.url_row = None
        self.guide_row = None
        if kind == XTREAM:
            self.host_row = self._entry(main, _("Server (URL)"), host, url=True)
            self.user_row = self._entry(main, _("Username"), username)
            self.pass_row = Adw.PasswordEntryRow(title=_("Password"), text=password)
            self.pass_row.connect("entry-activated", self._on_save)
            main.add(self.pass_row)
        elif kind == URL:
            self.url_row = self._entry(main, _("Playlist URL (M3U)"), url, url=True)
        if kind in (URL, FILE):
            self.guide_row = self._entry(main, _("Guide link (EPG) — optional"), guide,
                                         url=True)
            if guide_placeholder and not guide:
                self.guide_row.set_tooltip_text(guide_placeholder)
        page.add(main)

        self.backup_rows: list[Adw.EntryRow] = []
        if kind in (XTREAM, URL):
            group = Adw.PreferencesGroup(
                title=_("Backup servers (alternative DNS)"),
                description=_("Other addresses of the same provider, with the same login. "
                              "When the main one doesn't answer, AshyTV switches to the "
                              "next automatically."))
            saved = backups or []
            for i in range(failover.MAX_BACKUPS):
                row = self._entry(group, _("Backup server {n}").format(n=i + 1),
                                  saved[i] if i < len(saved) else "", url=True)
                self.backup_rows.append(row)
            page.add(group)

        view.set_content(page)
        self.set_child(view)
        self.set_focus(self.name_row)

    def _entry(self, group, title: str, text: str, url: bool = False) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title, text=text)
        if url:
            row.set_input_purpose(Gtk.InputPurpose.URL)
        row.connect("entry-activated", self._on_save)
        group.add(row)
        return row

    @staticmethod
    def _text(row) -> str:
        return row.get_text().strip() if row is not None else ""

    def values(self) -> dict:
        return {
            "name": self._text(self.name_row),
            "host": self._text(self.host_row),
            "username": self._text(self.user_row),
            "password": self._text(self.pass_row),
            "url": self._text(self.url_row),
            "backups": [self._text(r) for r in self.backup_rows],
            "guide": self._text(self.guide_row),
        }

    def validate(self, v: dict) -> str:
        """An error message, or "" when the values can be saved."""
        if self.kind == XTREAM and (v["host"] in ("", "http://", "https://")
                                    or not v["username"] or not v["password"]):
            return _("Fill in server, username and password.")
        if self.kind == URL and not v["url"].startswith(("http://", "https://")):
            return _("Enter a URL starting with http:// or https://.")
        if v["guide"] and not http.is_web_url(v["guide"]):
            return _("The guide link must be a web address (http or https).")
        return ""

    def _on_save(self, *_a) -> None:
        if not self.save_btn.get_sensitive():
            return
        v = self.values()
        problem = self.validate(v)
        if problem:
            self.finish(problem)
            return
        self.set_busy(True)
        self.emit("save", v)

    def set_busy(self, busy: bool) -> None:
        self.save_btn.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        if busy:
            self.error_group.set_visible(False)

    def finish(self, error: str | None) -> None:
        """The window is done with the values: close, or show what failed."""
        self.set_busy(False)
        if error is None:
            self.close()
            return
        self.error.set_text(error)
        self.error_group.set_visible(True)
