"""AshyTV AdwApplication."""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID, APP_VERSION
from .i18n import _
from .services.logos import logo_loader
from .services.settings import ACCENTS, Settings
from .services.storage import Storage
from .window import AshyTvWindow

_DATA = Path(__file__).parent / "data"
_CSS = _DATA / "style.css"
_SCHEMES = {
    "dark": Adw.ColorScheme.FORCE_DARK,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "system": Adw.ColorScheme.DEFAULT,
}
_SHORTCUTS = [
    (_("Navigation"), [("<Primary>f", _("Search")), ("<Primary>q", _("Quit"))]),
    (_("Player"), [("space", _("Pause/resume")), ("Up Down", _("Previous/next channel")),
                   ("Left Right", _("Rewind/forward 10 s")), ("f", _("Fullscreen")),
                   ("m", _("Mute")), ("plus minus", _("Volume")),
                   ("Escape", _("Exit fullscreen / close the player"))]),
]


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
            self.storage = Storage()
        if self.window is None:
            self.window = AshyTvWindow(storage=self.storage, settings=self.settings,
                                       application=self)
        self.window.present()

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
            self._css.load_from_path(str(_CSS))
            Gtk.StyleContext.add_provider_for_display(
                display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(
            display, self._dyn_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def _apply_theme(self, value) -> None:
        Adw.StyleManager.get_default().set_color_scheme(
            _SCHEMES.get(value, Adw.ColorScheme.FORCE_DARK))

    def _sync_scheme(self, *_args) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        scheme = Gtk.InterfaceColorScheme.DARK if dark else Gtk.InterfaceColorScheme.LIGHT
        for provider in (self._css, self._dyn_css):
            provider.set_property("prefers-color-scheme", scheme)

    def _apply_dynamic_css(self) -> None:
        s = self.settings
        _label, accent, accent2 = ACCENTS.get(s.get("accent"), ACCENTS["blue"])
        alpha = float(s.get("window_opacity")) if s.get("translucent") else 1.0
        css = [f":root {{ --ashy-accent: {accent}; --ashy-accent-2: {accent2}; "
               f"--ashy-window-alpha: {alpha:.2f}; }}"]
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
        about.present(self.window)

    def _on_shortcuts(self, *_args) -> None:
        dialog = Adw.ShortcutsDialog()
        for title, items in _SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for accel, label in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            dialog.add(section)
        dialog.present(self.window)
