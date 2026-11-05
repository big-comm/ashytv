"""Settings page — all options are persisted and applied."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ... import APP_VERSION
from ...i18n import _, ngettext
from ...services import http
from ...services.logos import logo_loader
from ...services.settings import ACCENTS, check_pin, hash_pin, set_autostart
from .. import shortcuts
from ..speed_test import SpeedTestGroup
from ._common import page_header

_CATS = [
    ("geral", _("General"), "emblem-system-symbolic"),
    ("aparencia", _("Appearance"), "applications-graphics-symbolic"),
    ("reproducao", _("Playback"), "media-playback-start-symbolic"),
    ("atalhos", _("Shortcuts"), "input-keyboard-symbolic"),
    ("pais", _("Parental Controls"), "system-lock-screen-symbolic"),
    ("rede", _("Network"), "network-wired-symbolic"),
    ("dados", _("Data"), "folder-download-symbolic"),
    ("sobre", _("About"), "dialog-information-symbolic"),
]
# Secure DNS: (setting value, label). "system" = off.
_DOH = [("system", _("Off (system DNS)")), ("cloudflare", "Cloudflare (1.1.1.1)"),
        ("google", "Google (8.8.8.8)")]
_THEMES = [("dark", _("Dark")), ("light", _("Light")), ("system", _("Follow system"))]
_REFRESH = [6, 12, 24, 48]


def recordings_dir() -> Path:
    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    base = Path(videos) if videos else Path.home() / "Videos"
    return base / "AshyTV"


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


class SettingsPage(Gtk.Box):
    __gtype_name__ = "AshySettingsPage"

    __gsignals__ = {
        "history-cleared": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # open the hidden channels and groups dialog
        "show-hidden": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, settings, storage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.settings = settings
        self.storage = storage
        self.add_css_class("ashy-settings")
        self.append(page_header(_("Settings"), _("Customize AshyTV your way")))

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        body.set_margin_start(28)
        body.set_margin_end(28)
        body.set_margin_bottom(22)
        body.set_vexpand(True)

        self.cats = Gtk.ListBox(hexpand=True)
        self.cats.add_css_class("ashy-settings-cat")
        self.cats.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.cats.connect("row-selected", self._on_cat)
        cat_panel = Gtk.Box(valign=Gtk.Align.START)
        cat_panel.add_css_class("ashy-panel")
        cat_panel.set_size_request(220, -1)
        cat_panel.set_hexpand(False)
        cat_panel.append(self.cats)
        body.append(cat_panel)

        self.stack = Gtk.Stack(hexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._geral(), "geral")
        self.stack.add_named(self._aparencia(), "aparencia")
        self.stack.add_named(self._reproducao(), "reproducao")
        self.stack.add_named(self._atalhos(), "atalhos")
        self.stack.add_named(self._pais(), "pais")
        self.stack.add_named(self._rede(), "rede")
        self.stack.add_named(self._dados(), "dados")
        self.stack.add_named(self._sobre(), "sobre")
        body.append(self.stack)
        self.append(body)

        for key, label, icon in _CATS:
            row = Gtk.ListBoxRow()
            row._key = key  # type: ignore[attr-defined]
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0))
            row.set_child(box)
            self.cats.append(row)
        self.cats.select_row(self.cats.get_row_at_index(0))

    def refresh(self) -> None:
        self._update_cache_row()

    def _on_cat(self, _l, row) -> None:
        if row is not None:
            self.stack.set_visible_child_name(row._key)  # type: ignore[attr-defined]
            if row._key == "dados":  # type: ignore[attr-defined]
                self._update_cache_row()

    # ---- helpers -------------------------------------------------------

    def _switch(self, group, key, title, subtitle="", on_change=None) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle,
                            active=bool(self.settings.get(key)))

        def changed(r, _p):
            self.settings.set(key, r.get_active())
            if on_change:
                on_change(r.get_active())

        row.connect("notify::active", changed)
        group.add(row)
        return row

    @staticmethod
    def _page(*groups) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage()
        for g in groups:
            page.add(g)
        return page

    # ---- categories ----------------------------------------------------

    def _geral(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("General"))
        self._switch(g, "autostart", _("Start with the system"),
                     _("Open AshyTV when you log in"), set_autostart)

        channels = Adw.PreferencesGroup(title=_("Channels"))
        self._switch(channels, "group_variants", _("Group qualities of the same channel"),
                     _("Shows “News FHD”, “News HD” and “News SD” as a single channel"))
        self._switch(channels, "sort_az", _("Sort from A to Z"),
                     _("Groups and channels in alphabetical order. Off, they keep "
                       "the playlist's order"))
        self._switch(channels, "channel_preview", _("Live preview"),
                     _("Plays the selected channel, muted, next to the channel list"))
        # Its own group, so it is easy to find: everything the user hid.
        hidden_group = Adw.PreferencesGroup(title=_("Hidden channels and groups"))
        self.hidden_row = Adw.ActionRow(title=_("Hidden channels and groups"),
                                        activatable=True, subtitle_lines=2)
        self.hidden_row.add_prefix(Gtk.Image(icon_name="view-conceal-symbolic"))
        self.hidden_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.hidden_row.connect("activated", lambda *_: self.emit("show-hidden"))
        hidden_group.add(self.hidden_row)
        self.update_hidden()

        g2 = Adw.PreferencesGroup(title=_("IPTV Playlists"))
        self._switch(g2, "auto_refresh", _("Reload playlists automatically"),
                     _("Updates channels and catalog when the app opens"))
        every = Adw.ComboRow(title=_("Check every"),
                             model=Gtk.StringList.new(
                                 [ngettext("{h} hour", "{h} hours", h).format(h=h)
                                  for h in _REFRESH]))
        cur = self.settings.get("refresh_hours")
        every.set_selected(_REFRESH.index(cur) if cur in _REFRESH else 1)
        every.connect("notify::selected", lambda r, _p: self.settings.set(
            "refresh_hours", _REFRESH[r.get_selected()]))
        g2.add(every)
        return self._page(g, channels, hidden_group, g2)

    def _rede(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(
            title=_("Secure DNS (DNS over HTTPS)"),
            description=_("Looks up server names over an encrypted connection, for more "
                          "privacy and fewer failures from the provider's DNS. If it "
                          "doesn't answer, the system DNS is used. HTTPS videos use the "
                          "system DNS."))
        doh = Adw.ComboRow(title=_("Secure DNS"),
                           model=Gtk.StringList.new([label for _k, label in _DOH]))
        keys = [k for k, _l in _DOH]
        cur = self.settings.get("doh")
        doh.set_selected(keys.index(cur) if cur in keys else 0)
        doh.connect("notify::selected",
                    lambda r, _p: self.settings.set("doh", keys[r.get_selected()]))
        g.add(doh)
        servers = Adw.PreferencesGroup(title=_("Backup servers"))
        self._switch(servers, "server_alerts", _("Desktop notifications"),
                     _("Tell me when a playlist's main server goes down or comes back"))
        return self._page(SpeedTestGroup(self.storage), g, servers)

    def _aparencia(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("Appearance"))
        theme = Adw.ComboRow(title=_("Theme"),
                             model=Gtk.StringList.new([t for _k, t in _THEMES]))
        keys = [k for k, _t in _THEMES]
        cur = self.settings.get("theme")
        theme.set_selected(keys.index(cur) if cur in keys else 0)
        theme.connect("notify::selected",
                      lambda r, _p: self.settings.set("theme", keys[r.get_selected()]))
        g.add(theme)

        accent_group = Adw.PreferencesGroup(
            title=_("Accent Color"),
            description=_("Choose the color used for buttons, selections and highlights."))
        swatches = Gtk.Box(homogeneous=True, spacing=4)
        swatches.add_css_class("ashy-panel")
        swatches.add_css_class("ashy-accent-card")
        first = None
        self._swatches: dict[str, Gtk.ToggleButton] = {}
        for key, (label, _color, _light) in ACCENTS.items():
            btn = Gtk.ToggleButton(tooltip_text=label, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER)
            btn.add_css_class("ashy-swatch")
            btn.add_css_class(f"ashy-accent-{key}")  # color generated in application.py
            if first is None:
                first = btn
            else:
                btn.set_group(first)
            btn.set_active(self.settings.get("accent") == key)
            btn.connect("toggled", self._on_accent, key)
            self._swatches[key] = btn
            swatches.append(btn)
        self._paint_swatches()
        accent_group.add(swatches)

        g2 = Adw.PreferencesGroup(title=_("Transparency"),
                                  description=_("Makes the window background translucent "
                                                "(depends on the compositor)."))
        self._switch(g2, "translucent", _("Translucent window"))
        opacity = Adw.SpinRow.new_with_range(60, 100, 2)
        opacity.set_title(_("Background opacity (%)"))
        opacity.set_value(round(self.settings.get("window_opacity") * 100))
        opacity.connect("notify::value", lambda r, _p: self.settings.set(
            "window_opacity", r.get_value() / 100))
        g2.add(opacity)

        g3 = Adw.PreferencesGroup(title=_("Content"))
        self._switch(g3, "show_logos", _("Show logos and covers"),
                     _("Turn off to save data"))
        return self._page(g, accent_group, g2, g3)

    def _reproducao(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("Playback"),
                                 description=_("Applies from the next video you open."))
        self._switch(g, "auto_quality", _("Automatic quality"),
                     _("Switches between a channel's qualities to match your connection"))
        self._switch(g, "hwdec", _("Hardware acceleration"),
                     _("Decodes video on the GPU (VA-API/VDPAU/NVDEC)"))
        buf = Adw.SpinRow.new_with_range(5, 120, 5)
        buf.set_title(_("Network buffer (seconds)"))
        buf.set_subtitle(_("More buffer = fewer stutters, but more delay when changing channels"))
        buf.set_value(self.settings.get("cache_secs"))
        buf.connect("notify::value", lambda r, _p: self.settings.set(
            "cache_secs", int(r.get_value())))
        g.add(buf)

        return self._page(g)

    def _atalhos(self) -> Gtk.Widget:
        groups = []
        for title, items in shortcuts.KEYBOARD:
            group = Adw.PreferencesGroup(title=title)
            for accels, label in items:
                row = Adw.ActionRow(title=label)
                row.add_suffix(shortcuts.keys_widget(accels))
                group.add(row)
            groups.append(group)
        pad = Adw.PreferencesGroup(
            title=_("Game controller"),
            description=_("Connect a controller to use AshyTV like a TV remote."))
        for buttons, label in shortcuts.GAMEPAD:
            row = Adw.ActionRow(title=label)
            row.add_suffix(shortcuts.keys_widget(buttons, raw=True))
            pad.add(row)
        groups.append(pad)
        return self._page(*groups)

    def _pais(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("Parental Controls"))
        self.adult_row = Adw.SwitchRow(
            title=_("Hide adult content"),
            subtitle=_("Hides categories with terms like “adult”, “XXX”, “+18”"),
            active=bool(self.settings.get("hide_adult")))
        self.adult_row.connect("notify::active", self._on_adult)
        g.add(self.adult_row)

        g2 = Adw.PreferencesGroup(
            title=_("PIN"), description=_("With a PIN set, it is required to "
                                           "show adult content."))
        self.pin_row = Adw.PasswordEntryRow(title=_("New PIN (4+ digits)"))
        self.pin_row.set_show_apply_button(True)
        self.pin_row.connect("apply", self._on_pin_apply)
        g2.add(self.pin_row)
        clear = Adw.ButtonRow(title=_("Remove PIN"))
        clear.connect("activated", self._on_pin_clear)
        g2.add(clear)
        return self._page(g, g2)

    def _dados(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("Data"))
        self.cache_row = Adw.ActionRow(title=_("Clear cache"),
                                       subtitle=_("Removes downloaded logos, covers "
                                                  "and guide data"))
        btn = Gtk.Button(label=_("Clear"), valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._clear_cache)
        self.cache_row.add_suffix(btn)
        g.add(self.cache_row)

        hist = Adw.ActionRow(title=_("Clear history"),
                             subtitle=_("Forgets “Continue Watching” and recent items"))
        hbtn = Gtk.Button(label=_("Clear"), valign=Gtk.Align.CENTER)
        hbtn.connect("clicked", self._clear_history)
        hist.add_suffix(hbtn)
        g.add(hist)

        rec = Adw.ActionRow(title=_("Recordings folder"), subtitle=str(recordings_dir()))
        obtn = Gtk.Button(icon_name="folder-videos-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text=_("Open folder"))
        obtn.add_css_class("flat")
        obtn.connect("clicked", self._open_recordings)
        rec.add_suffix(obtn)
        g.add(rec)
        return self._page(g)

    def _sobre(self) -> Gtk.Widget:
        g = Adw.PreferencesGroup(title=_("About"))
        g.add(Adw.ActionRow(title="AshyTV",
                            subtitle=_("Version {version}").format(version=APP_VERSION)))
        g.add(Adw.ActionRow(title="CommunityBIG", subtitle="communitybig.org"))
        g.add(Adw.ActionRow(
            title=_("Disclaimer"),
            subtitle=_("AshyTV does not provide or contain any content: it only plays "
                       "the playlists you add."), subtitle_lines=3))
        g.add(Adw.ActionRow(
            title="TMDB",
            subtitle=_("Cast data: TMDB. This product uses the TMDB API but is not endorsed "
                       "or certified by TMDB."), subtitle_lines=3))
        tour = Adw.ButtonRow(title=_("Guided tour"))
        tour.connect("activated", lambda *_: self.activate_action("app.tour", None))
        g.add(tour)
        about = Adw.ButtonRow(title=_("More information"))
        about.connect("activated", lambda *_: self.activate_action("app.about", None))
        g.add(about)
        return self._page(g)

    def _on_accent(self, button: Gtk.ToggleButton, key: str) -> None:
        if button.get_active():
            self.settings.set("accent", key)
        self._paint_swatches()

    def _paint_swatches(self) -> None:
        # Mark the selected one with a ✓ (besides the CSS ring).
        for btn in self._swatches.values():
            if btn.get_active():
                btn.set_icon_name("object-select-symbolic")
            else:
                btn.set_child(None)

    # ---- actions ---------------------------------------------------------

    def update_hidden(self) -> None:
        n = len(self.storage.hidden_entries())  # as many as the dialog lists
        self.hidden_row.set_subtitle(
            ngettext("{n} hidden item · Click to see it and show it again",
                     "{n} hidden items · Click to see them and show them again",
                     n).format(n=n) if n else
            _("Nothing hidden · Right-click a group or channel to hide it"))

    def _update_cache_row(self) -> None:
        # Walking thousands of cached logos is slow: do it off the main thread.
        def worker():
            size = _dir_size(http.cache_root())
            GLib.idle_add(self._show_cache_size, size)

        threading.Thread(target=worker, daemon=True).start()

    def _show_cache_size(self, size: int) -> bool:
        self.cache_row.set_subtitle(
            _("Removes downloaded logos, covers and guide data — {size}").format(
                size=GLib.format_size(size)))
        return GLib.SOURCE_REMOVE

    def _clear_cache(self, button) -> None:
        # Thousands of files: deleting them on the main thread froze the window.
        button.set_sensitive(False)
        logo_loader.clear_memory()

        def worker():
            shutil.rmtree(http.cache_root(), ignore_errors=True)
            GLib.idle_add(done)

        def done():
            button.set_sensitive(True)
            self._update_cache_row()
            self.emit("toast", _("Cache cleared."))
            return GLib.SOURCE_REMOVE

        threading.Thread(target=worker, daemon=True).start()

    def _clear_history(self, _b) -> None:
        self.storage.clear_history()
        self.emit("history-cleared")
        self.emit("toast", _("History cleared."))

    def _open_recordings(self, _b) -> None:
        path = recordings_dir()
        path.mkdir(parents=True, exist_ok=True)
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(str(path)))
        root = self.get_root()
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None, None)

    def _on_adult(self, row, _p) -> None:
        hide = row.get_active()
        if hide or not self.settings.get("parental_pin"):
            self.settings.set("hide_adult", hide)
            return
        # To show adult content with a PIN set, ask for the PIN.
        row.handler_block_by_func(self._on_adult)
        row.set_active(True)
        row.handler_unblock_by_func(self._on_adult)
        def unlock():
            self._set_adult_row(False)
            self.settings.set("hide_adult", False)

        self._ask_pin(unlock)

    def _set_adult_row(self, value: bool) -> None:
        self.adult_row.handler_block_by_func(self._on_adult)
        self.adult_row.set_active(value)
        self.adult_row.handler_unblock_by_func(self._on_adult)

    def _ask_pin(self, on_ok) -> None:
        dialog = Adw.AlertDialog(heading=_("Enter PIN"),
                                 body=_("Required to show adult content."))
        entry = Gtk.PasswordEntry(activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Unlock"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")

        def response(_d, r):
            if r != "ok":
                return
            if check_pin(self.settings.get("parental_pin") or "", entry.get_text()):
                on_ok()
            else:
                self.emit("toast", _("Incorrect PIN."))

        dialog.connect("response", response)
        dialog.present(self)

    def _on_pin_apply(self, row) -> None:
        pin = row.get_text().strip()
        if len(pin) < 4 or not pin.isdigit():
            self.emit("toast", _("The PIN must have at least 4 digits."))
            return
        def save():
            self.settings.set("parental_pin", hash_pin(pin))
            row.set_text("")
            self.emit("toast", _("PIN set."))

        if self.settings.get("parental_pin"):
            self._ask_pin(save)  # changing the PIN needs the current one
        else:
            save()

    def _on_pin_clear(self, _row) -> None:
        if not self.settings.get("parental_pin"):
            return
        def remove():
            self.settings.set("parental_pin", "")
            self.emit("toast", _("PIN removed."))

        self._ask_pin(remove)
