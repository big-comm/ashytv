"""Playlists page — add/manage playlists (M3U, Xtream, URL)."""

from __future__ import annotations

import locale
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ...i18n import _, ngettext
from ...services import failover, free_tv, http
from ...services.xtream_client import XtreamProvider, parse_provider
from ..servers_dialog import ServersDialog, status_text
from ._common import page_header


_GUIDE_LINK_ERROR = _("The guide link must be a web address (http or https).")


def _fmt_time(ts: float) -> str:
    return GLib.DateTime.new_from_unix_local(int(ts)).format("%x %R")


def backup_entries(parent: Gtk.Box, placeholder: str) -> list[Gtk.Entry]:
    """'Backup servers (alternative DNS)': up to MAX_BACKUPS URL entries."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8)
    hint = Gtk.Label(label=_("Other addresses of the same provider, with the same login. "
                             "When the main one doesn't answer, AshyTV switches to the "
                             "next automatically."), xalign=0, wrap=True)
    hint.add_css_class("ashy-faint")
    box.append(hint)
    entries = []
    for i in range(failover.MAX_BACKUPS):
        entry = Gtk.Entry(placeholder_text=placeholder, input_purpose=Gtk.InputPurpose.URL,
                          tooltip_text=_("Backup server {n}").format(n=i + 1))
        entry.add_css_class("ashy-entry")
        box.append(entry)
        entries.append(entry)
    expander = Gtk.Expander(label=_("Backup servers (alternative DNS)"), child=box)
    parent.append(expander)
    return entries


class ListsPage(Gtk.Box):
    __gtype_name__ = "AshyListsPage"

    __gsignals__ = {
        # name, source (file/URL), backup URLs, guide link ("" = the playlist's)
        "import-requested": (GObject.SignalFlags.RUN_FIRST, None, (str, str, object, str)),
        # playlist id, guide link ("" = the playlist's own url-tvg)
        "guide-link-changed": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        # name, host, username, password, backup servers
        "xtream-requested": (GObject.SignalFlags.RUN_FIRST, None,
                             (str, str, str, str, object)),
        # playlist id, backup servers (edited in the Servers dialog)
        "servers-changed": (GObject.SignalFlags.RUN_FIRST, None, (int, object)),
        "check-servers": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "refresh-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # playlist id, enabled
        "enabled-changed": (GObject.SignalFlags.RUN_FIRST, None, (int, bool)),
    }

    def __init__(self, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.storage = storage
        self._file_path = ""
        self._switches: dict[int, Gtk.Switch] = {}
        self._refocus: int | None = None  # playlist whose switch gets the focus back
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
        for key, label in [("free", _("Free TV")), ("file", _("M3U File")),
                           ("xtream", _("Xtream Codes")), ("url", _("URL"))]:
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
        self.x_backups = backup_entries(xbox, "backup-server.com:8080")
        self.stack.add_named(xbox, "xtream")

        # url
        ubox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.url_entry = self._field(ubox, _("Playlist URL (M3U)"), "network-server-symbolic",
                                     Gtk.Entry(placeholder_text="http://…/playlist.m3u",
                                               input_purpose=Gtk.InputPurpose.URL))
        self.u_backups = backup_entries(ubox, "…/playlist.m3u")
        self.stack.add_named(ubox, "url")

        # free TV
        self.stack.add_named(self._free_tv_box(), "free")
        inner.append(self.stack)

        # M3U file or URL: the guide may come from elsewhere.
        self.guide_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.guide_entry = self._field(self.guide_box, _("Guide link (EPG) — optional"),
                                       "x-office-calendar-symbolic",
                                       Gtk.Entry(placeholder_text="https://…/guide.xml",
                                                 input_purpose=Gtk.InputPurpose.URL))
        self.guide_entry.set_tooltip_text(_("An XMLTV guide; it replaces the one named "
                                            "in the playlist (url-tvg)"))
        inner.append(self.guide_box)

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

        self._tabs["free"].set_active(True)

    def _free_tv_box(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._free_names = free_tv.country_names()
        countries = sorted(self._free_names,
                           key=lambda c: locale.strxfrm(self._free_names[c]))
        self._free_codes = [free_tv.ALL, *countries]
        labels = [_("All countries"), *(self._free_names[c] for c in countries)]
        self.free_drop = Gtk.DropDown(
            model=Gtk.StringList.new(labels), enable_search=True,
            expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.free_drop.connect("notify::selected", self._on_free_country)
        self.free_drop.set_selected(self._free_codes.index(free_tv.default_country()))
        self._field_widget(box, _("TV channels from"), "tv-symbolic", self.free_drop)

        self.free_catalog = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        row = Gtk.Box(spacing=12)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title = Gtk.Label(label=_("Free movies and cartoons"), xalign=0)
        title.add_css_class("ashy-field-title")
        sub = Gtk.Label(label=_("Classic films in the public domain and Blender's "
                                "open movies"), xalign=0, wrap=True)
        sub.add_css_class("ashy-faint")
        texts.append(title)
        texts.append(sub)
        row.append(texts)
        row.append(self.free_catalog)
        self._field_widget(box, "", "ashy-film-symbolic", row)

        note = Gtk.Label(
            label=_("Only free channels that broadcasters make publicly available, and "
                    "films with an open licence or in the public domain. AshyTV is a "
                    "player: it does not host any of them."),
            xalign=0, wrap=True)
        note.add_css_class("ashy-faint")
        box.append(note)
        return box

    def _free_tv_name(self) -> str:
        return free_tv.playlist_name(self._free_codes[self.free_drop.get_selected()],
                                     self._free_names)

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
        img = Gtk.Image(icon_name=icon, valign=Gtk.Align.END if title else Gtk.Align.CENTER)
        img.add_css_class("ashy-field-icon")
        row.append(img)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        if title:
            lbl = Gtk.Label(label=title, xalign=0)
            lbl.add_css_class("ashy-field-title")
            col.append(lbl)
        col.append(widget)
        row.append(col)
        parent.append(row)

    def focus_content(self) -> bool:
        """Entering the page from the sidebar lands on the first saved playlist,
        or on the selected tab of the form."""
        row = self.existing.get_row_at_index(0)
        if self.existing_box.get_visible() and row is not None:
            return row.grab_focus()
        tab = next((b for b in self._tabs.values() if b.get_active()), None)
        return tab is not None and tab.grab_focus()

    def invalidate(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.existing.remove_all()
        self._switches = {}
        playlists = self.storage.list_playlists()
        for pl in playlists:
            self.existing.append(self._playlist_row(pl))
        self.existing_box.set_visible(bool(playlists))
        self.add_head.set_label(_("Add Another Playlist") if playlists else _("Add Playlist"))
        # The rows are rebuilt: give the keyboard focus back to the switch
        # the user just flipped.
        refocus, self._refocus = self._refocus, None
        if refocus in self._switches:
            self._switches[refocus].grab_focus()

    def _on_switch(self, switch: Gtk.Switch, playlist_id: int) -> None:
        self._refocus = playlist_id
        self.emit("enabled-changed", playlist_id, switch.get_active())
        GLib.idle_add(self._refresh_once)

    def _refresh_once(self) -> bool:
        self.refresh()
        return GLib.SOURCE_REMOVE

    def clear_form(self) -> None:
        """Empty the "add" form after a playlist was saved."""
        for entry in (self.name_entry, self.host_entry, self.user_entry,
                      self.pass_entry, self.url_entry, self.guide_entry,
                      *self.x_backups, *self.u_backups):
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
        enabled = bool(pl["enabled"])
        if not enabled:
            parts.insert(0, _("Disabled"))
        if pl["updated_at"]:
            parts.append(_("updated {when}").format(when=_fmt_time(pl["updated_at"])))
        meta = self.storage.playlist_meta(pl["id"])
        coverage = meta.get("epg_coverage")
        if (isinstance(coverage, list) and len(coverage) == 2
                and all(isinstance(n, int) for n in coverage) and coverage[1]):
            parts.append(_("Guide: {n} of {m} channels").format(n=coverage[0], m=coverage[1]))
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
                         else "tv-symbolic" if free else "view-list-symbolic",
                         valign=Gtk.Align.CENTER)
        icon.add_css_class("ashy-field-icon")
        row.add_prefix(icon)
        if not enabled:
            row.add_css_class("ashy-row-off")
        switch = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER,
                            tooltip_text=_("Show this playlist's channels"))
        switch.connect("notify::active", lambda sw, _p: self._on_switch(sw, pl["id"]))
        self._switches[pl["id"]] = switch
        servers = self._servers(pl)
        if servers:
            row.add_suffix(self._server_button(pl, servers))
        if kind == "m3u" and not free:
            guide = Gtk.Button(icon_name="x-office-calendar-symbolic",
                               valign=Gtk.Align.CENTER,
                               tooltip_text=_("Guide link (EPG)"))
            guide.add_css_class("flat")
            guide.connect("clicked", lambda *_: self._edit_guide_link(pl, meta))
            row.add_suffix(guide)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=_("Refresh playlist"))
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.emit("refresh-requested", pl["id"]))
        delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text=_("Remove playlist"))
        delete.add_css_class("flat")
        delete.connect("clicked", lambda *_: self._confirm_delete(pl))
        row.add_suffix(switch)
        row.add_suffix(refresh)
        row.add_suffix(delete)
        return row

    def _servers(self, pl) -> list[str]:
        """Main + backup URLs of a playlist ([] for files and Free TV)."""
        if pl["kind"] == "xtream":
            try:
                main = XtreamProvider.from_json(pl["source"], "").host
            except (ValueError, KeyError):
                return []
        elif "://" in pl["source"] and not free_tv.is_free_tv(pl["source"]):
            main = pl["source"]
        else:
            return []
        return [main, *failover.clean_backups(self.storage.backups(pl["id"]), main)]

    def _server_button(self, pl, servers: list[str]) -> Gtk.Widget:
        """One status dot per server; opens the Servers dialog."""
        health = failover.health_list(self.storage.server_health(pl["id"]), servers)
        active = pl["active_server"] or 0
        dots = Gtk.Box(spacing=3)
        for i, h in enumerate(health):
            dot = Gtk.Label(label="●")
            dot.add_css_class("ashy-server-dot")
            dot.add_css_class(h.status or "unknown")
            if i == active:
                dot.add_css_class("active")
            dots.append(dot)
        box = Gtk.Box(spacing=6)
        box.append(Gtk.Image(icon_name="network-server-symbolic"))
        box.append(dots)
        cur = health[active] if active < len(health) else health[0]
        tip = _("Servers: {status} — click for details").format(
            status=status_text(cur) if cur.status else _("not checked yet"))
        btn = Gtk.Button(child=box, valign=Gtk.Align.CENTER, tooltip_text=tip)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda *_: self._open_servers(pl, servers))
        return btn

    def _open_servers(self, pl, servers: list[str]) -> None:
        dialog = ServersDialog(self.storage, pl["id"], pl["name"], servers,
                               pl["active_server"] or 0)
        dialog.connect("save", lambda _d, backups: self.emit("servers-changed",
                                                             pl["id"], backups))
        dialog.connect("check", lambda _d: self.emit("check-servers", pl["id"]))
        dialog.present(self)

    def _edit_guide_link(self, pl, meta: dict) -> None:
        own = meta.get("epg_url", "")
        entry = Gtk.Entry(text=meta.get("epg_override", ""), hexpand=True,
                          placeholder_text=own or "https://…/guide.xml",
                          input_purpose=Gtk.InputPurpose.URL, activates_default=True)
        error = Gtk.Label(xalign=0, wrap=True, visible=False)
        error.add_css_class("error")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        box.append(error)
        body = _("An XMLTV guide for “{name}”. It replaces the one named in the "
                 "playlist; leave it empty to use the playlist's own.").format(name=pl["name"])
        dialog = Adw.AlertDialog(heading=_("Guide link (EPG) — optional"), body=body,
                                 extra_child=box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def check(*_a):
            text = entry.get_text().strip()
            ok = not text or http.is_web_url(text)
            error.set_text("" if ok else _GUIDE_LINK_ERROR)
            error.set_visible(not ok)
            dialog.set_response_enabled("save", ok)

        def response(_d, answer):
            if answer == "save":
                text = entry.get_text().strip()
                if text != meta.get("epg_override", ""):
                    self.emit("guide-link-changed", pl["id"], text)

        entry.connect("changed", check)
        dialog.connect("response", response)
        dialog.present(self)

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
            self.guide_box.set_visible(key in ("file", "url"))
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
        guide = self.guide_entry.get_text().strip() if key in ("file", "url") else ""
        if guide and not http.is_web_url(guide):
            self.set_status(_GUIDE_LINK_ERROR, error=True)
            return
        if key == "file":
            if not self._file_path:
                self.set_status(_("Choose an .m3u file."), error=True)
                return
            self.emit("import-requested", name or _("My Playlist"), self._file_path, [],
                      guide)
        elif key == "url":
            url = self.url_entry.get_text().strip()
            if not url.startswith(("http://", "https://")):
                self.set_status(_("Enter a URL starting with http:// or https://."),
                                error=True)
                return
            self.emit("import-requested", name or _("My Playlist"), url,
                      [e.get_text().strip() for e in self.u_backups], guide)
        elif key == "free":
            source = free_tv.make_source(self._free_codes[self.free_drop.get_selected()],
                                         self.free_catalog.get_active())
            self.emit("import-requested", name or self._free_tv_name(), source, [], "")
        elif key == "xtream":
            host = self.host_entry.get_text().strip()
            user = self.user_entry.get_text().strip()
            pwd = self.pass_entry.get_text().strip()
            if host in ("", "http://", "https://") or not user or not pwd:
                self.set_status(_("Fill in server, username and password."), error=True)
                return
            self.emit("xtream-requested", name or _("Xtream Provider"), host, user, pwd,
                      [e.get_text().strip() for e in self.x_backups])
