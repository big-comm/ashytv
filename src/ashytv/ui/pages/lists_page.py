"""Playlists page — add/manage playlists (M3U, Xtream, URL)."""

from __future__ import annotations

import locale
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ...i18n import _, ngettext
from ...services import free_tv
from ...services.xtream_client import XtreamProvider, parse_provider
from ._common import page_header


def _fmt_time(ts: float) -> str:
    return GLib.DateTime.new_from_unix_local(int(ts)).format("%x %R")


class ListsPage(Gtk.Box):
    __gtype_name__ = "AshyListsPage"

    __gsignals__ = {
        # name, source (file/URL)
        "import-requested": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # name, host, username, password
        "xtream-requested": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),
        "refresh-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self._file_path = ""
        self._busy = False

        self.append(page_header(_("Playlists"),
                                _("Manage your playlists or add a new one")))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        outer.set_margin_start(28)
        outer.set_margin_end(28)
        outer.set_margin_bottom(24)

        # The saved playlists come first; the form to add another one below.
        self.add_head = Gtk.Label(label=_("Add Playlist"), xalign=0)
        self.add_head.add_css_class("ashy-section-title")
        outer.append(self.add_head)
        top = Gtk.Box(spacing=18)
        outer.append(top)

        # ---- form ----
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        form.add_css_class("ashy-panel")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        inner.set_margin_top(18)
        inner.set_margin_bottom(18)
        inner.set_margin_start(18)
        inner.set_margin_end(18)
        form.append(inner)

        # Tabs (single selection guaranteed by the toggle button group).
        tabs = Gtk.Box(homogeneous=True, halign=Gtk.Align.CENTER)
        tabs.add_css_class("ashy-tabs")
        self._tabs: dict[str, Gtk.ToggleButton] = {}
        first = None
        for key, label in [("file", _("M3U File")), ("xtream", _("Xtream Codes")),
                           ("url", _("URL")), ("free", _("Free TV"))]:
            btn = Gtk.ToggleButton(label=label)
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_tab, key)
            self._tabs[key] = btn
            tabs.append(btn)
        inner.append(tabs)

        self.name_entry = self._field(inner, _("Playlist name"), "view-list-symbolic",
                                      Gtk.Entry(placeholder_text=_("My Playlist")))

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               vhomogeneous=False)

        # file
        fbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        pick = Gtk.Button(label=_("Choose .m3u file…"))
        pick.connect("clicked", self._choose_file)
        self.file_label = Gtk.Label(label=_("No file selected"), xalign=0,
                                    ellipsize=3)
        self.file_label.add_css_class("ashy-faint")
        fb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        fb.append(pick)
        fb.append(self.file_label)
        self._field_widget(fbox, _("File"), "document-open-symbolic", fb)
        self.stack.add_named(fbox, "file")

        # xtream
        xbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.host_entry = self._field(xbox, _("Server (URL)"), "network-server-symbolic",
                                      Gtk.Entry(placeholder_text="http://your-server.com:8080",
                                                input_purpose=Gtk.InputPurpose.URL))
        self.user_entry = self._field(xbox, _("Username"), "avatar-default-symbolic",
                                      Gtk.Entry(placeholder_text=_("your_username")))
        self.pass_entry = self._field(xbox, _("Password"), "dialog-password-symbolic",
                                      Gtk.PasswordEntry(placeholder_text=_("your_password"),
                                                        show_peek_icon=True))
        paste = Gtk.Button(label=_("Paste provider message"), halign=Gtk.Align.START)
        paste.add_css_class("flat")
        paste.set_tooltip_text(_("Paste the provider's welcome message: server, "
                                 "username and password are filled in automatically"))
        paste.connect("clicked", self._paste_detect)
        xbox.append(paste)
        self.stack.add_named(xbox, "xtream")

        # url
        ubox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.url_entry = self._field(ubox, _("Playlist URL (M3U)"), "network-server-symbolic",
                                     Gtk.Entry(placeholder_text="http://…/playlist.m3u",
                                               input_purpose=Gtk.InputPurpose.URL))
        self.stack.add_named(ubox, "url")

        # free TV (iptv-org)
        self.stack.add_named(self._free_tv_box(), "free")
        inner.append(self.stack)

        self.status = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.status.add_css_class("ashy-faint")
        inner.append(self.status)

        self.save_btn = Gtk.Button()
        sbox = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.save_spinner = Adw.Spinner(visible=False)
        sbox.append(self.save_spinner)
        sbox.append(Gtk.Label(label=_("Save and Connect")))
        self.save_btn.set_child(sbox)
        self.save_btn.add_css_class("pill-accent")
        self.save_btn.connect("clicked", self._save)
        inner.append(self.save_btn)
        top.append(form)

        # ---- "Secure Connection" card ----
        secure = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         valign=Gtk.Align.START)
        secure.add_css_class("ashy-panel")
        secure.set_size_request(240, -1)
        sin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=28, margin_bottom=28, margin_start=18, margin_end=18)
        icon = Gtk.Image(icon_name="channel-secure-symbolic", pixel_size=34,
                         halign=Gtk.Align.CENTER)
        icon.add_css_class("ashy-secure-icon")
        t = Gtk.Label(label=_("Secure Connection"))
        t.add_css_class("ashy-poster-title")
        d = Gtk.Label(label=_("Your information is stored only on your device. "
                              "Passwords are kept in the system keyring."),
                      wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=26)
        d.add_css_class("ashy-faint")
        sin.append(icon)
        sin.append(t)
        sin.append(d)
        secure.append(sin)
        top.append(secure)

        # ---- existing playlists (on top) ----
        self.existing_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        head = Gtk.Label(label=_("Your Playlists"), xalign=0)
        head.add_css_class("ashy-section-title")
        self.existing_box.append(head)
        self.existing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.existing.add_css_class("boxed-list")
        self.existing_box.append(self.existing)
        outer.prepend(self.existing_box)

        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=700)
        clamp.set_child(outer)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(clamp)
        self.append(sw)

        self._tabs["xtream"].set_active(True)

    def _free_tv_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        names = free_tv.country_names()
        countries = sorted(names, key=lambda c: locale.strxfrm(names[c]))
        self._free_codes = [free_tv.ALL_BY_COUNTRY, free_tv.ALL_BY_CATEGORY, *countries]
        labels = [_("All countries"), _("All countries, grouped by category"),
                  *(names[c] for c in countries)]
        self._free_labels = dict(zip(self._free_codes, labels, strict=True))
        self.free_drop = Gtk.DropDown(
            model=Gtk.StringList.new(labels), enable_search=True,
            expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        default = free_tv.default_country() or free_tv.ALL_BY_COUNTRY
        self.free_drop.connect("notify::selected", self._on_free_country)
        self.free_drop.set_selected(self._free_codes.index(default))
        self._field_widget(box, _("Channels from"), "tv-symbolic",
                           self.free_drop)
        note = Gtk.Label(
            label=_("Free channels from iptv-org, a community list of streams that "
                    "broadcasters make publicly available. AshyTV does not host or "
                    "control them, and some may be offline."),
            xalign=0, wrap=True)
        note.add_css_class("ashy-faint")
        box.append(note)
        box.append(Gtk.LinkButton(uri=free_tv.PROJECT_URL, label=_("About iptv-org"),
                                  halign=Gtk.Align.START))
        return box

    def _free_tv_name(self) -> str:
        code = self._free_codes[self.free_drop.get_selected()]
        return _("Free TV – {where}").format(where=self._free_labels[code])

    def _on_free_country(self, *_a) -> None:
        if self.stack.get_visible_child_name() == "free":
            self.name_entry.set_placeholder_text(self._free_tv_name())

    # ---- fields with icon (as in the mockup) -----------------------------

    def _field(self, parent, title, icon, entry):
        entry.add_css_class("ashy-entry")
        entry.set_hexpand(True)
        entry.connect("activate", self._save)
        self._field_widget(parent, title, icon, entry)
        return entry

    def _field_widget(self, parent, title, icon, widget):
        row = Gtk.Box(spacing=12)
        img = Gtk.Image(icon_name=icon, valign=Gtk.Align.END)
        img.add_css_class("ashy-field-icon")
        row.append(img)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        lbl = Gtk.Label(label=title, xalign=0)
        lbl.add_css_class("ashy-field-title")
        col.append(lbl)
        col.append(widget)
        row.append(col)
        parent.append(row)

    def invalidate(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.existing.remove_all()
        playlists = self.storage.list_playlists()
        for pl in playlists:
            self.existing.append(self._playlist_row(pl))
        self.existing_box.set_visible(bool(playlists))
        self.add_head.set_label(_("Add Another Playlist") if playlists else _("Add Playlist"))

    def clear_form(self) -> None:
        """Empty the "add" form after a playlist was saved."""
        for entry in (self.name_entry, self.host_entry, self.user_entry,
                      self.pass_entry, self.url_entry):
            entry.set_text("")
        self._file_path = ""
        self.file_label.set_text(_("No file selected"))

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.save_btn.set_sensitive(not busy)
        self.save_spinner.set_visible(busy)
        self.set_status(message)

    def set_status(self, message: str, error: bool = False) -> None:
        self.status.set_text(message)
        self.status.set_visible(bool(message))
        if error:
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")

    def _playlist_row(self, pl) -> Gtk.Widget:
        kind = pl["kind"]
        count = self.storage.count_channels(pl["id"])
        free = kind == "m3u" and free_tv.is_free_tv(pl["source"])
        parts = [_("Xtream Codes") if kind == "xtream" else _("Free TV") if free
                 else _("M3U"),
                 ngettext("{n} item", "{n} items", count).format(n=count)]
        if pl["updated_at"]:
            parts.append(_("updated {when}").format(when=_fmt_time(pl["updated_at"])))
        meta = self.storage.playlist_meta(pl["id"])
        if meta.get("exp_date"):
            exp = int(meta["exp_date"])
            left = exp - time.time()
            when = _fmt_time(exp)
            parts.append(_("expires {when}").format(when=when) if left > 0
                         else _("expired on {when}").format(when=when))
        row = Adw.ActionRow(title=GLib.markup_escape_text(pl["name"]),
                            subtitle=" • ".join(parts))
        if kind == "xtream":
            try:
                host = XtreamProvider.from_json(pl["source"], "").host
                row.set_subtitle(row.get_subtitle() + f"\n{host}")
                row.set_subtitle_lines(2)
            except (ValueError, KeyError):
                pass
        icon = Gtk.Image(icon_name="network-server-symbolic" if kind == "xtream"
                         else "tv-symbolic" if free else "view-list-symbolic")
        icon.add_css_class("ashy-field-icon")
        row.add_prefix(icon)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Refresh playlist"))
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.emit("refresh-requested", pl["id"]))
        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove playlist"))
        delete.add_css_class("flat")
        delete.connect("clicked", lambda *_: self._confirm_delete(pl))
        row.add_suffix(refresh)
        row.add_suffix(delete)
        return row

    def _confirm_delete(self, pl) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Remove playlist?"),
            body=_("“{name}” and all its channels, favorites and history "
                   "will be removed from this device.").format(name=pl["name"]))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Remove"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_delete_response, pl["id"])
        dialog.present(self)

    def _on_delete_response(self, _dialog, response: str, playlist_id: int) -> None:
        if response == "delete":
            self.emit("delete-requested", playlist_id)

    # ---- tabs ----------------------------------------------------------

    def _on_tab(self, button: Gtk.ToggleButton, key: str) -> None:
        if button.get_active():
            self.stack.set_visible_child_name(key)
            self.set_status("")
            self.name_entry.set_placeholder_text(
                self._free_tv_name() if key == "free" else _("My Playlist"))

    # ---- file -------------------------------------------------------

    def _choose_file(self, _b) -> None:
        dialog = Gtk.FileDialog(title=_("Select M3U playlist"))
        filt = Gtk.FileFilter()
        filt.set_name(_("M3U Playlists"))
        filt.add_pattern("*.m3u")
        filt.add_pattern("*.m3u8")
        filt.add_mime_type("audio/x-mpegurl")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        root = self.get_root()
        dialog.open(root if isinstance(root, Gtk.Window) else None, None,
                    self._file_chosen)

    def _file_chosen(self, dialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None:
            return
        self._file_path = gfile.get_path() or ""
        self.file_label.set_text(gfile.get_basename() or self._file_path)
        if not self.name_entry.get_text().strip():
            self.name_entry.set_text(gfile.get_basename() or "")

    # ---- paste / xtream ------------------------------------------------

    def _paste_detect(self, _b) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().read_text_async(None, self._clip_text)

    def _clip_text(self, clipboard, result) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            text = None
        prov = parse_provider(text) if text else None
        if prov is None:
            self.set_status(_("Couldn't find server, username and password in the copied text."),
                            error=True)
            return
        self.host_entry.set_text(prov.host)
        self.user_entry.set_text(prov.username)
        self.pass_entry.set_text(prov.password)
        self.set_status(_("Provider details filled in. Check them and click "
                          "“Save and Connect”."))

    # ---- save --------------------------------------------------------

    def _save(self, *_a) -> None:
        if self._busy:
            return
        name = self.name_entry.get_text().strip()
        key = self.stack.get_visible_child_name()
        if key == "file":
            if not self._file_path:
                self.set_status(_("Choose an .m3u file."), error=True)
                return
            self.emit("import-requested", name or _("My Playlist"), self._file_path)
        elif key == "url":
            url = self.url_entry.get_text().strip()
            if not url.startswith(("http://", "https://")):
                self.set_status(_("Enter a URL starting with http:// or https://."),
                                error=True)
                return
            self.emit("import-requested", name or _("My Playlist"), url)
        elif key == "free":
            code = self._free_codes[self.free_drop.get_selected()]
            self.emit("import-requested", name or self._free_tv_name(),
                      free_tv.playlist_url(code))
        elif key == "xtream":
            host = self.host_entry.get_text().strip()
            user = self.user_entry.get_text().strip()
            pwd = self.pass_entry.get_text().strip()
            if host in ("", "http://", "https://") or not user or not pwd:
                self.set_status(_("Fill in server, username and password."), error=True)
                return
            self.emit("xtream-requested", name or _("Xtream Provider"), host, user, pwd)
