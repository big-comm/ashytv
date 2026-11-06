"""AshyTV AdwApplication."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID, APP_VERSION, compat
from .i18n import _
from .services.logos import logo_loader
from .services.settings import ACCENTS, Settings
from .services import system_deps
from .services.storage import Storage
from .ui import shortcuts

if TYPE_CHECKING:  # the window imports python-mpv, which needs libmpv
    from .window import AshyTvWindow

_DATA = Path(__file__).parent / "data"
_CSS = _DATA / "style.css"
_SCHEMES = {
    "dark": Adw.ColorScheme.FORCE_DARK,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "system": Adw.ColorScheme.DEFAULT,
}
# The window waits at most this long for the database (an import may hold it).
_UI_DB_TIMEOUT = 3.0


class AshyTvApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.storage: Storage | None = None
        self.settings: Settings | None = None
        self.window: AshyTvWindow | None = None
        self._dyn_css = Gtk.CssProvider()
        self._css = Gtk.CssProvider()
        # GTK < 4.20: settings-driven custom properties, baked into the CSS
        self._css_overrides: dict[str, str] = {}

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        self.settings = Settings()
        display = Gdk.Display.get_default()
        if display is not None:
            # App icons (logo and symbolic) straight from the data/ folder.
            Gtk.IconTheme.get_for_display(display).add_search_path(str(_DATA / "icons"))
        self._load_css()
        self._apply_theme(self.settings.get("theme"))
        self._apply_dynamic_css()
        logo_loader.enabled = bool(self.settings.get("show_logos"))

        # Each provider evaluates @media (prefers-color-scheme) on its own:
        # it follows libadwaita's effective light/dark.
        Adw.StyleManager.get_default().connect("notify::dark", self._sync_scheme)
        self._sync_scheme()
        self.settings.connect("theme", self._apply_theme)
        for key in ("accent", "translucent", "window_opacity"):
            self.settings.connect(key, lambda _v: self._apply_dynamic_css())
        self.settings.connect("show_logos", self._on_show_logos)

        watch = Gio.SimpleAction.new("watch-reminder", GLib.VariantType.new("s"))
        watch.connect("activate", self._on_watch_reminder)
        self.add_action(watch)

        for name, callback, accels in (
            ("about", self._on_about, []),
            ("tour", lambda *_: self.window and self.window.start_tour(), []),
            ("shortcuts", self._on_shortcuts, ["<Primary>question"]),
            ("quit", lambda *_: self._quit(), ["<Primary>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self) -> None:
        if self.storage is None:
            try:
                self.storage = Storage(timeout=_UI_DB_TIMEOUT)
            except sqlite3.Error as exc:
                self._fatal(_("AshyTV could not open its database"), str(exc))
                return
        if self.window is None:
            if not system_deps.libmpv_loads():
                self._ask_install_libmpv()
                return
            from .window import AshyTvWindow

            self.window = AshyTvWindow(storage=self.storage, settings=self.settings,
                                       application=self)
            GLib.idle_add(self._check_codecs)
        self.window.present()

    def _ask_install_libmpv(self) -> None:
        """The host has no libmpv (the AppImage does not bundle it)."""
        from .ui.install_dialog import InstallLibmpvWindow

        for window in self.get_windows():
            if isinstance(window, InstallLibmpvWindow):
                window.present()
                return
        InstallLibmpvWindow(system_deps.install_plan(), application=self).present()

    def _check_codecs(self) -> bool:
        """Warn once per run when the host's FFmpeg lacks H.264/HEVC."""
        missing = system_deps.missing_codecs()
        if missing and self.window is not None:
            dialog = Adw.AlertDialog(
                heading=_("Some channels will not play"),
                body=_("The system's video codecs lack {codecs}, used by most TV "
                       "channels.").format(codecs=", ".join(c.upper() for c in missing))
                + "\n\n" + system_deps.codecs_hint())
            dialog.add_response("close", _("Close"))
            dialog.present(self.window)
        return GLib.SOURCE_REMOVE

    def _fatal(self, title: str, detail: str) -> None:
        """Say why the app can't start instead of opening nothing."""
        dialog = Adw.AlertDialog(heading=title, body=detail)
        dialog.add_response("close", _("Close"))
        window = Adw.ApplicationWindow(application=self, default_width=480,
                                       default_height=200)
        dialog.connect("response", lambda *_a: self.quit())
        window.present()
        dialog.present(window)

    def do_shutdown(self) -> None:
        if self.storage is not None:
            self.storage.close()
        Adw.Application.do_shutdown(self)

    def _on_watch_reminder(self, _action, key) -> None:
        self.activate()  # make sure the window exists (e.g. started from the notification)
        if self.window is not None:
            self.window.watch_reminder(key.get_string())

    def _quit(self) -> None:
        if self.window is not None:
            self.window.close()
        self.quit()

    # ---- appearance ----------------------------------------------------

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        if _CSS.exists():
            if compat.MODERN_CSS:
                self._css.load_from_path(str(_CSS))
            else:
                self._load_flat_css()
            Gtk.StyleContext.add_provider_for_display(
                display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(
            display, self._dyn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def _apply_theme(self, value) -> None:
        Adw.StyleManager.get_default().set_color_scheme(
            _SCHEMES.get(value, Adw.ColorScheme.FORCE_DARK))

    def _load_flat_css(self) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        text = _CSS.read_text(encoding="utf-8")
        self._css.load_from_string(compat.flatten_css(text, dark, self._css_overrides))

    def _sync_scheme(self, *_args) -> None:
        if not compat.MODERN_CSS:
            if _CSS.exists():
                self._load_flat_css()
            return
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = Gtk.InterfaceColorScheme.DARK if dark else Gtk.InterfaceColorScheme.LIGHT
        for provider in (self._css, self._dyn_css):
            provider.set_property("prefers-color-scheme", scheme)

    def _apply_dynamic_css(self) -> None:
        s = self.settings
        _label, accent, accent2 = ACCENTS.get(s.get("accent"), ACCENTS["blue"])
        alpha = float(s.get("window_opacity")) if s.get("translucent") else 1.0
        root = {"--ashy-accent": accent, "--ashy-accent-2": accent2,
                "--ashy-window-alpha": f"{alpha:.2f}"}
        css = []
        if compat.MODERN_CSS:
            css.append(":root { " + " ".join(f"{k}: {v};" for k, v in root.items()) + " }")
        else:
            self._css_overrides = root
            if _CSS.exists():
                self._load_flat_css()
        # Color swatches in Settings.
        css += [f"button.ashy-accent-{k} {{ background: {c}; }}"
                for k, (_l, c, _c2) in ACCENTS.items()]
        self._dyn_css.load_from_string("\n".join(css))

    def _on_show_logos(self, value) -> None:
        logo_loader.enabled = bool(value)

    # ---- dialogs -------------------------------------------------------

    def _on_about(self, *_args) -> None:
        about = Adw.AboutDialog(
            application_name="AshyTV",
            application_icon=APP_ID,
            version=APP_VERSION,
            developer_name="CommunityBIG",
            comments=_("A modern IPTV player for the Linux desktop.\n"
                       "Import your own playlist — no content is provided by the app."),
            website="https://communitybig.org",
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_legal_section(
            "TMDB", None, Gtk.License.CUSTOM,
            _("Cast data: TMDB. This product uses the TMDB API but is not endorsed "
              "or certified by TMDB."))
        about.present(self.window)

    def _on_shortcuts(self, *_args) -> None:
        if not hasattr(Adw, "ShortcutsDialog"):  # libadwaita < 1.8
            self._show_shortcuts_legacy()
            return
        dialog = Adw.ShortcutsDialog()
        for title, items in shortcuts.KEYBOARD:
            section = Adw.ShortcutsSection(title=title)
            for accels, label in items:
                section.add(Adw.ShortcutsItem(
                    title=label, accelerator=shortcuts.dialog_accelerator(accels)))
            dialog.add(section)
        dialog.present(self.window)

    def _show_shortcuts_legacy(self) -> None:
        """Same list as a preferences dialog, for libadwaita < 1.8."""
        dialog = Adw.PreferencesDialog(title=_("Keyboard Shortcuts"))
        page = Adw.PreferencesPage()
        for title, items in shortcuts.KEYBOARD:
            group = Adw.PreferencesGroup(title=title)
            for accels, label in items:
                row = Adw.ActionRow(title=label)
                row.add_suffix(Gtk.ShortcutLabel(
                    accelerator=shortcuts.dialog_accelerator(accels),
                    valign=Gtk.Align.CENTER))
                group.add(row)
            page.add(group)
        dialog.add(page)
        dialog.present(self.window)
