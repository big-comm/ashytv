"""AshyTV main window — shell with a side navigation."""

from __future__ import annotations

import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from . import APP_ID
from .i18n import _, ngettext
from .models import Channel, Episode, MediaType, PlayItem, Variant
from .services import epg, http, importer, secrets
from .services.quality import quality_label
from .services.gamepad import GamepadInput
from .services.reminders import Reminder, ReminderBook
from .services.storage import Storage, collapse_variants
from .services.xtream_client import XtreamProvider, normalize_host, series_episodes
from .ui import keynav
from .ui.detail_dialog import DetailDialog
from .ui.nav_sidebar import COMPACT_WIDTH, FULL_WIDTH, NavSidebar
from .ui.pages.catalog_page import CatalogPage
from .ui.pages.channels_page import ChannelsPage
from .ui.pages.home_page import HomePage
from .ui.pages.lists_page import ListsPage
from .ui.pages.recordings_page import RecordingsPage
from .ui.pages.settings_page import SettingsPage
from .ui.schedule_dialog import schedule_dialog
from .ui.welcome_view import WelcomeView

_SEARCHABLE = {"channels", "movies", "series", "favorites"}
_EPISODE_CACHE_SECS = 6 * 3600


class AshyTvWindow(Adw.ApplicationWindow):
    __gtype_name__ = "AshyTvWindow"

    def __init__(self, storage: Storage, settings, **kwargs):
        super().__init__(**kwargs)
        self.storage = storage
        self.settings = settings
        self.set_title("AshyTV")
        self.set_icon_name(APP_ID)
        self.set_default_size(1280, 800)
        self.add_css_class("ashy-window")

        self.player = None
        self.mini = None
        self._epg_guide = None
        self._epg_source = ""
        self._current = "home"
        self._providers: dict[int, XtreamProvider] = {}
        self._zap: list | Gio.ListModel | None = None
        self._zap_index = 0
        self._prev_zap_index: int | None = None
        self._series_ctx: tuple[Channel, list[Episode]] | None = None
        self._importing = False
        self._quiet_id = 0
        self._focus_before_player: Gtk.Widget | None = None
        self._refresh_queue: list[int] = []
        self._fetched_series: dict[int, float] = {}

        self._load_providers()

        # ---- pages ----
        self.pages: dict[str, Gtk.Widget] = {}
        self.page_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    transition_duration=150)
        self._build_pages()

        # ---- sidebar ----
        self.nav = NavSidebar()
        self.nav.connect("navigate", lambda _w, key: self.navigate(key))
        self.nav.connect("compact-changed", self._on_nav_compact)

        # ---- header (spans the full width, as in the mockup) ----
        header = Adw.HeaderBar()
        self.sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                            tooltip_text=_("Menu"), visible=False)
        header.pack_start(self.sidebar_btn)
        brand = Gtk.Box(spacing=8)
        brand.append(Gtk.Image(icon_name=APP_ID, pixel_size=22))
        title = Gtk.Label(label="AshyTV")
        title.add_css_class("ashy-app-title")
        brand.append(title)
        header.pack_start(brand)

        self.search = Gtk.SearchEntry(placeholder_text=_("Search…"), visible=False)
        self.search.set_size_request(360, -1)
        self.search.connect("search-changed", self._on_search)
        self.search.connect("stop-search", lambda *_: self.search_btn.set_active(False))
        header.set_title_widget(self.search)

        menu = Gio.Menu()
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About AshyTV"), "app.about")
        menu.append(_("Quit"), "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text=_("Main Menu")))
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text=_("Search (Ctrl+F)"))
        self.search_btn.connect("toggled", self._on_search_toggled)
        header.pack_end(self.search_btn)

        self.banner = Adw.Banner(revealed=False)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.banner)
        content.append(self.page_stack)
        self.page_stack.set_vexpand(True)

        self.split = Adw.OverlaySplitView(show_sidebar=True, sidebar_width_fraction=0.18,
                                          min_sidebar_width=FULL_WIDTH,
                                          max_sidebar_width=FULL_WIDTH)
        self.split.set_sidebar(self.nav)
        self.split.set_content(content)
        self.split.bind_property("show-sidebar", self.sidebar_btn, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL
                                 | GObject.BindingFlags.SYNC_CREATE)
        self.split.connect("notify::collapsed", self._on_collapsed)
        self.nav.set_compact(bool(self.settings.get("sidebar_compact")))

        shell = Adw.ToolbarView()
        shell.add_top_bar(header)
        shell.set_content(self.split)

        # ---- welcome + shell + player (root stack) ----
        self.welcome = WelcomeView()
        self.welcome.connect("start", self._on_start)

        self.toasts = Adw.ToastOverlay()
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.add_named(self.welcome, "welcome")
        self.root_stack.add_named(shell, "shell")
        self.toasts.set_child(self.root_stack)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self._install_keys()
        self.gamepad = GamepadInput(self.handle_remote_key)
        self.reminders = ReminderBook(self.settings.get("reminders"))
        if self.pages["channels"].guide is not None:
            self.pages["channels"].guide.reminders = self.reminders
        GLib.timeout_add_seconds(20, self._check_reminders)
        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        self._connect_settings()

        if self.storage.has_playlists():
            self.root_stack.set_visible_child_name("shell")
            self.navigate("home")
            self._autoload_epg()
            GLib.timeout_add_seconds(3, self._auto_refresh)
        else:
            self.root_stack.set_visible_child_name("welcome")

    # ---- page construction ----------------------------------------

    def _build_pages(self) -> None:
        home = HomePage(self.storage)
        home.connect("play", self._on_play)
        home.connect("resume", self._on_resume)
        home.connect("navigate", lambda _w, k: self.navigate(k))

        channels = ChannelsPage(self.storage, MediaType.LIVE)
        channels.connect("play", self._on_play)
        channels.connect("favorite-changed", self._on_favorite_changed)
        channels.connect("show-schedule", self._on_show_schedule)
        channels.connect("hide-group", self._on_hide_group)
        channels.connect("remind", self._on_remind)
        channels.connect("move-group", self._on_move_group)


        movies = CatalogPage(self.storage, MediaType.MOVIE, _("Movies"),
                             _("The best movies for you"))
        movies.connect("play", self._on_play)
        movies.connect("favorite-changed", self._on_favorite_changed)

        series = CatalogPage(self.storage, MediaType.SERIES, _("Series"),
                             _("Your favorite series in one place"))
        series.connect("play", self._on_play)
        series.connect("favorite-changed", self._on_favorite_changed)

        favorites = ChannelsPage(self.storage, favorites_only=True)
        favorites.connect("play", self._on_play)
        favorites.connect("favorite-changed", self._on_favorite_changed)
        favorites.connect("show-schedule", self._on_show_schedule)

        recordings = RecordingsPage()
        recordings.connect("play", self._on_play)

        lists = ListsPage(self.storage)
        lists.connect("import-requested", self._on_import_requested)
        lists.connect("xtream-requested", self._on_xtream_requested)
        lists.connect("refresh-requested", lambda _w, pid: self._refresh_playlist(pid))
        lists.connect("delete-requested", self._on_delete_requested)
        lists.connect("enabled-changed", self._on_playlist_enabled)

        settings = SettingsPage(self.settings, self.storage)
        settings.connect("toast", lambda _w, msg: self._toast(msg))
        settings.connect("history-cleared", lambda *_: self.pages["home"].invalidate())

        self.pages = {
            "home": home, "channels": channels,
            "movies": movies, "series": series, "favorites": favorites,
            "recordings": recordings, "lists": lists, "settings": settings,
        }
        for key, page in self.pages.items():
            self.page_stack.add_named(page, key)

    # ---- navigation -----------------------------------------------------

    def navigate(self, key: str) -> None:
        if key not in self.pages:
            return
        self._current = key
        self.nav.set_active(key)
        self.page_stack.set_visible_child_name(key)
        page = self.pages[key]
        if hasattr(page, "refresh"):
            page.refresh()
        searchable = key in _SEARCHABLE
        self.search_btn.set_visible(searchable)
        if not searchable:
            self.search_btn.set_active(False)
        elif hasattr(page, "set_search"):
            # The new page filters by the text already in the search box.
            page.set_search(self.search.get_text())
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_start(self, _w) -> None:
        self.root_stack.set_visible_child_name("shell")
        self.navigate("lists" if not self.storage.has_playlists() else "home")

    def _on_nav_compact(self, _nav, compact: bool) -> None:
        width = COMPACT_WIDTH if compact else FULL_WIDTH
        self.split.set_min_sidebar_width(width)
        self.split.set_max_sidebar_width(width)
        self.settings.set("sidebar_compact", compact)

    def _on_collapsed(self, split, _p) -> None:
        collapsed = split.get_collapsed()
        self.sidebar_btn.set_visible(collapsed)
        split.set_show_sidebar(not collapsed)

    def _on_search_toggled(self, button) -> None:
        active = button.get_active()
        self.search.set_visible(active)
        if active:
            self.search.grab_focus()
        else:
            self.search.set_text("")

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        page = self.pages.get(self._current)
        if page is not None and hasattr(page, "set_search"):
            page.set_search(entry.get_text())

    def _invalidate_pages(self, *keys: str) -> None:
        for key in keys or self.pages:
            page = self.pages[key]
            if hasattr(page, "invalidate"):
                page.invalidate()

    # ---- providers / URLs ---------------------------------------------

    def _load_providers(self) -> None:
        self._providers.clear()
        for row in self.storage.list_playlists():
            if row["kind"] != "xtream":
                continue
            try:
                prov = XtreamProvider.from_json(row["source"])
                if not prov.password:
                    prov.password = secrets.lookup_password(prov.host, prov.username) or ""
                self._providers[int(row["id"])] = prov
            except (ValueError, KeyError):
                continue

    def _resolve(self, url: str, playlist_id: int) -> str:
        """Relative Xtream provider path -> URL with credentials."""
        if "://" in url or url.startswith("/"):
            return url
        prov = self._providers.get(playlist_id)
        return prov.stream_url(url) if prov else url

    # ---- playback ----------------------------------------------------

    def _on_play(self, page, channel: Channel) -> None:
        if channel.media_type in (MediaType.SERIES, MediaType.MOVIE) and channel.playlist_id:
            self._open_detail(channel)
            return
        # Zap through the page's visible list (Channels); on other pages,
        # through all live channels.
        zap = page.filter_model if page is self.pages["channels"] else None
        self._play_channel(channel, zap)

    def _live_channels(self) -> list[Channel]:
        chans = self.storage.channels(MediaType.LIVE)
        return collapse_variants(chans) if self.storage.group_variants else chans

    def _live_item(self, ch: Channel, number: int) -> PlayItem:
        """PlayItem for a live channel, with its quality variants when grouped."""
        variants = []
        if self.storage.group_variants:
            variants = [Variant(label=quality_label(v.quality),
                                url=self._resolve(v.url, v.playlist_id),
                                user_agent=v.user_agent, referrer=v.referrer)
                        for v in self.storage.variants(ch)]
        title = ch.base_name if len(variants) > 1 else ch.display_name
        return PlayItem(
            title=title, url=self._resolve(ch.url, ch.playlist_id), key=ch.url,
            media_type=ch.media_type, playlist_id=ch.playlist_id, logo=ch.logo,
            tvg_id=ch.tvg_id, number=number, channel=ch,
            user_agent=ch.user_agent, referrer=ch.referrer,
            variants=variants if len(variants) > 1 else [])

    def _play_channel(self, channel: Channel, zap=None, start: float = 0) -> None:
        if channel.media_type is MediaType.LIVE and zap is None:
            zap = self._live_channels()
        self._zap = zap if channel.media_type is MediaType.LIVE else None
        self._series_ctx = None
        self._zap_index = self._find_index(channel)
        self._prev_zap_index = None
        number = self._zap_index + 1 if self._zap is not None else 0
        if channel.media_type is MediaType.LIVE:
            self._start(self._live_item(channel, number), can_zap=self._zap is not None)
            return
        if channel.media_type is MediaType.MOVIE and not start:
            start = self.storage.resume_position(channel.playlist_id, channel.url)
        item = PlayItem(
            title=channel.display_name,
            url=self._resolve(channel.url, channel.playlist_id),
            key=channel.url, media_type=channel.media_type,
            playlist_id=channel.playlist_id, logo=channel.logo, tvg_id=channel.tvg_id,
            number=number, start=start, channel=channel,
            subtitle=" • ".join(p for p in (channel.year, channel.genre) if p),
            user_agent=channel.user_agent, referrer=channel.referrer)
        self._start(item, can_zap=self._zap is not None)

    def _play_episode(self, series: Channel, episode: Episode, episodes: list[Episode],
                      start: float | None = None) -> None:
        self._zap = None
        self._series_ctx = (series, episodes)
        if start is None:
            start = self.storage.resume_position(series.playlist_id, episode.url)
        item = PlayItem(
            title=series.display_name,
            subtitle=f"{episode.label} — {episode.title}" if episode.title else episode.label,
            url=self._resolve(episode.url, series.playlist_id), key=episode.url,
            media_type=MediaType.SERIES, playlist_id=series.playlist_id,
            logo=series.logo, start=start, channel=series)
        self._start(item, can_zap=len(episodes) > 1)

    def _start(self, item: PlayItem, can_zap: bool) -> None:
        if not self._ensure_player():
            return
        if self.mini is not None:
            self._close_mini(stop=False)
        if self.root_stack.get_visible_child_name() == "shell":
            self._focus_before_player = self.get_focus()  # restored on close
        self.player.play(item, can_zap=can_zap)
        self.root_stack.set_visible_child_name("player")
        if item.is_live:
            self._record(item, 0, 0)

    def _find_index(self, channel: Channel) -> int:
        if self._zap is None:
            return 0
        if isinstance(self._zap, Gio.ListModel):
            for i in range(self._zap.get_n_items()):
                if self._zap.get_item(i).channel.url == channel.url:
                    return i
            return 0
        for i, ch in enumerate(self._zap):
            if ch.url == channel.url:
                return i
        return 0

    def _zap_item(self, index: int) -> Channel:
        if isinstance(self._zap, Gio.ListModel):
            return self._zap.get_item(index).channel
        return self._zap[index]

    def _zap_len(self) -> int:
        if isinstance(self._zap, Gio.ListModel):
            return self._zap.get_n_items()
        return len(self._zap or [])

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True
        try:
            # Deferred import: without python-mpv/libEGL the app still opens.
            from .ui.player_view import PlayerView
        except (ImportError, OSError) as exc:
            self._toast(_("Player unavailable: install mpv and python-mpv ({error})")
                        .format(error=exc))
            return False
        self.player = PlayerView(self.settings)
        self.player.connect("closed", self._on_player_closed)
        self.player.connect("zap", self._on_zap)
        self.player.connect("toggle-fullscreen", self._on_toggle_fullscreen)
        self.player.connect("favorite-toggled", self._on_player_fav)
        self.player.connect("progress", lambda _p, item, pos, dur:
                            self._record(item, pos, dur))
        self.player.connect("ended", self._on_ended)
        self.player.connect("pip-requested", self._on_pip)
        self.player.connect("channel-list", self._on_channel_list)
        self.player.connect("zap-to", lambda _p, index: self._zap_to(index))
        self.player.connect("zap-previous", lambda *_: self._zap_previous())
        self.player.connect("toast", lambda _p, msg: self._toast(msg))
        if self._epg_guide is not None:
            self.player.set_guide(self._epg_guide)
        self.root_stack.add_named(self.player, "player")
        return True

    def _on_zap(self, _p, delta: int) -> None:
        if self._series_ctx is not None:
            series, episodes = self._series_ctx
            cur = self.player.item.key if self.player.item else ""
            idx = next((i for i, e in enumerate(episodes) if e.url == cur), 0)
            nxt = idx + delta
            if 0 <= nxt < len(episodes):
                self._play_episode(series, episodes[nxt], episodes)
            return
        n = self._zap_len()
        if n:
            self._zap_to((self._zap_index + delta) % n)

    def _zap_to(self, index: int) -> None:
        """Tune the channel at `index` of the zapping list (0-based)."""
        n = self._zap_len()
        if not 0 <= index < n:
            self.player.flash(_("No channel {number}").format(number=index + 1))
            return
        if index == self._zap_index and self.player.item is not None:
            return
        self._prev_zap_index = self._zap_index
        self._zap_index = index
        item = self._live_item(self._zap_item(index), index + 1)
        self.player.play(item, can_zap=True)
        self._record(item, 0, 0)

    def _zap_previous(self) -> None:
        if self._prev_zap_index is not None:
            self._zap_to(self._prev_zap_index)

    def _on_channel_list(self, _p) -> None:
        n = self._zap_len()
        if n:
            channels = [self._zap_item(i) for i in range(n)]
            self.player.show_channel_list(channels, self._zap_index, self._epg_guide)

    def _on_ended(self, _p, item: PlayItem) -> None:
        if self._series_ctx is None:
            return
        _series, episodes = self._series_ctx
        idx = next((i for i, e in enumerate(episodes) if e.url == item.key), -1)
        if 0 <= idx < len(episodes) - 1:
            self._toast(_("Next episode…"))
            self._on_zap(None, 1)

    def _record(self, item: PlayItem, pos: float, dur: float) -> None:
        if not item.playlist_id:
            return
        self.storage.record_history(
            item.playlist_id, item.key, item.media_type, item.title,
            subtitle=item.subtitle, logo=item.logo, tvg_id=item.tvg_id,
            position=pos, duration=dur,
            parent=item.channel.url if item.media_type is MediaType.SERIES
            and item.channel else "")
        self.pages["home"].invalidate()

    def _on_resume(self, _page, row) -> None:
        mt = MediaType(row["media_type"])
        pid = row["playlist_id"]
        if mt is MediaType.SERIES and row["parent"]:
            series = self.storage.find_channel(pid, row["parent"])
            if series is None:
                return
            self._with_episodes(series, lambda eps: self._resume_episode(series, eps,
                                                                         row["url"]),
                                lambda exc: self._toast(
                                    _("Failed to open: {error}").format(error=exc)))
            return
        ch = self.storage.find_channel(pid, row["url"])
        if ch is None:
            self._toast(_("This item is no longer in the playlist."))
            return
        self._play_channel(ch)

    def _resume_episode(self, series, episodes, url) -> None:
        ep = next((e for e in episodes if e.url == url), None)
        if ep is not None:
            self._play_episode(series, ep, episodes)

    def _on_toggle_fullscreen(self, _p) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_a) -> None:
        if self.player is not None:
            self.player.set_fullscreen(self.is_fullscreen())

    def _on_player_closed(self, _p) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        self.root_stack.set_visible_child_name("shell")
        page = self.pages.get(self._current)
        if hasattr(page, "refresh"):
            page.refresh()
        GLib.idle_add(self._restore_focus)

    def _restore_focus(self) -> bool:
        """Back from the player: the keyboard focus returns where it was."""
        widget, self._focus_before_player = self._focus_before_player, None
        if not (widget is not None and widget.get_mapped() and widget.grab_focus()):
            self.focus_page() or self.nav.focus_active()
        return GLib.SOURCE_REMOVE

    def _on_player_fav(self, _p, item: PlayItem) -> None:
        ch = item.channel
        self.storage.set_favorite(ch, not ch.favorite)
        self._on_favorite_changed(None, ch)
        self._toast(_("Added to favorites") if ch.favorite else _("Removed from favorites"))

    def _on_favorite_changed(self, source, _channel) -> None:
        for key in ("home", "channels", "favorites", "movies", "series"):
            if self.pages[key] is not source:
                self.pages[key].invalidate()

    # ---- mini player ---------------------------------------------------

    def _on_pip(self, _p) -> None:
        from .ui.mini_player import MiniPlayer

        if self.mini is not None or self.player.item is None:
            return
        if self.is_fullscreen():
            self.unfullscreen()
        video = self.player.detach_video()
        self.mini = MiniPlayer(video, self.player.item.title, application=self.get_application())
        self.mini.connect("expand", lambda *_: self._close_mini(stop=False, show=True))
        self.mini.connect("stop", lambda *_: self._close_mini(stop=True))
        self.root_stack.set_visible_child_name("shell")
        self.mini.present()

    def _close_mini(self, stop: bool, show: bool = False) -> None:
        mini, self.mini = self.mini, None
        if mini is None:
            return
        mini.release_video()
        self.player.attach_video()
        if stop:
            self.player.close()
        elif show:
            self.root_stack.set_visible_child_name("player")
            self.present()
        mini.destroy()

    # ---- details / series ---------------------------------------------

    def _open_detail(self, channel: Channel) -> None:
        dialog = DetailDialog(self.storage, channel, self._with_episodes)
        dialog.connect("play", self._on_detail_play)
        dialog.connect("favorite-changed", self._on_favorite_changed)
        dialog.present(self)

    def _on_detail_play(self, _d, channel: Channel, episode: Episode | None) -> None:
        if episode is None:
            self._play_channel(channel)
            return
        self._play_episode(channel, episode, self.storage.episodes(channel.id) or [episode])

    def _with_episodes(self, series: Channel, on_done, on_error) -> None:
        """Episodes from the database (M3U/cache) or the Xtream API, in the background."""
        cached = self.storage.episodes(series.id)
        fresh = time.time() - self._fetched_series.get(series.id, 0) < _EPISODE_CACHE_SECS
        prov = self._providers.get(series.playlist_id)
        if cached and (fresh or prov is None or not series.ext_id):
            on_done(cached)
            return
        if prov is None or not series.ext_id:
            on_done([])
            return

        def worker():
            try:
                eps = series_episodes(prov, series.ext_id)
            except Exception as exc:
                GLib.idle_add(self._episodes_done, series, None, exc, on_done, on_error)
                return
            GLib.idle_add(self._episodes_done, series, eps, None, on_done, on_error)

        threading.Thread(target=worker, daemon=True).start()

    def _episodes_done(self, series, eps, exc, on_done, on_error) -> bool:
        if exc is not None:
            cached = self.storage.episodes(series.id)
            if cached:
                on_done(cached)
            else:
                on_error(exc)
            return GLib.SOURCE_REMOVE
        # A playlist refresh may have replaced the row: look the series up again.
        current = self.storage.find_channel(series.playlist_id, series.url)
        if current is None:
            on_done(eps)
            return GLib.SOURCE_REMOVE
        self.storage.replace_episodes(current.id, eps)
        self._fetched_series[current.id] = time.time()
        on_done(self.storage.episodes(current.id))
        return GLib.SOURCE_REMOVE

    def _on_show_schedule(self, _page, channel: Channel) -> None:
        schedule_dialog(channel, self._epg_guide,
                        lambda ch: self._play_channel(ch)).present(self)

    # ---- playlist import ---------------------------------------------

    def _begin_import(self, message: str, quiet_id: int = 0) -> bool:
        """Start an import. `quiet_id` marks an automatic background refresh
        of that playlist: no banner or form status, only a toast at the end."""
        if self._importing:
            if not quiet_id:
                self._toast(_("Please wait: an import is already in progress."))
            return False
        self._importing = True
        self._quiet_id = quiet_id
        self.pages["lists"].set_busy(True, "" if quiet_id else message)
        if not quiet_id:
            self.banner.set_title(message)
            self.banner.set_revealed(True)
        return True

    def _import_progress(self, message: str) -> None:
        if self._quiet_id:
            return
        self.pages["lists"].set_status(message)
        self.banner.set_title(message)

    def _on_import_requested(self, _w, name: str, source: str) -> None:
        if not self._begin_import(_("Importing playlist…")):
            return
        db_path = self.storage.path
        importer.run_async(
            lambda progress: importer.import_m3u(db_path, name, source, progress),
            self._import_done, self._import_error, self._import_progress)

    def _on_xtream_requested(self, _w, name: str, host: str, user: str, pwd: str) -> None:
        provider = XtreamProvider(host=normalize_host(host), username=user, password=pwd)
        self._run_xtream_import(name, provider)

    def _run_xtream_import(self, name: str, provider: XtreamProvider,
                           quiet_id: int = 0) -> None:
        if not self._begin_import(_("Connecting to the Xtream provider…"), quiet_id):
            return
        db_path = self.storage.path
        importer.run_async(
            lambda progress: importer.import_xtream(db_path, name, provider, progress),
            self._import_done, self._import_error, self._import_progress)

    def _refresh_playlist(self, playlist_id: int, quiet: bool = False) -> bool:
        """Refresh a playlist; returns False when it could not be started."""
        row = self.storage.get_playlist(playlist_id)
        if row is None:
            return False
        quiet_id = playlist_id if quiet else 0
        if row["kind"] == "xtream":
            prov = self._providers.get(playlist_id)
            if prov is None or not prov.password:
                if not quiet:
                    self._toast(_("Provider password not found; add the playlist again."))
                return False
            self._run_xtream_import(row["name"], prov, quiet_id)
        else:
            if not self._begin_import(_("Updating playlist…"), quiet_id):
                return False
            db_path, name, source = self.storage.path, row["name"], row["source"]
            importer.run_async(
                lambda progress: importer.import_m3u(db_path, name, source, progress),
                self._import_done, self._import_error, self._import_progress)
        return True

    def _import_done(self, result) -> None:
        quiet_id, self._quiet_id = self._quiet_id, 0
        self._importing = False
        self.banner.set_revealed(False)
        self._load_providers()
        self._invalidate_pages()
        self._epg_source = ""
        self._autoload_epg(force_id=result.playlist_id)
        if quiet_id:
            self.pages["lists"].set_busy(False)
            row = self.storage.get_playlist(quiet_id)
            if row is not None:
                self._toast(_("Playlist “{name}” updated.").format(name=row["name"]))
            self._next_auto_refresh()
            return
        self.pages["lists"].set_busy(False, ngettext(
            "{n} item imported.", "{n} items imported.", result.count).format(n=result.count))
        self._toast(ngettext("Playlist ready: {n} item.", "Playlist ready: {n} items.",
                             result.count).format(n=result.count))
        self.pages["lists"].clear_form()
        if self.root_stack.get_visible_child_name() != "player":
            self.root_stack.set_visible_child_name("shell")
        if self._current == "lists":
            self.navigate("channels")
        self._next_auto_refresh()

    def _import_error(self, exc: Exception) -> None:
        quiet_id, self._quiet_id = self._quiet_id, 0
        self._importing = False
        self.banner.set_revealed(False)
        msg = http.describe_error(exc)
        self.pages["lists"].set_busy(False)
        if quiet_id:
            # Background refresh: keep the old data, don't retry until the
            # next interval and don't put the error in the "add" form.
            row = self.storage.get_playlist(quiet_id)
            if row is not None:
                self.storage.update_playlist_meta(quiet_id, refresh_failed_at=int(time.time()))
                self._toast(_("Could not update “{name}”: {error}").format(
                    name=row["name"], error=msg))
            self._next_auto_refresh()
            return
        self.pages["lists"].set_status(_("Failed: {error}").format(error=msg), error=True)
        self._toast(_("Import failed: {error}").format(error=msg))
        self._next_auto_refresh()

    def _on_delete_requested(self, _w, playlist_id: int) -> None:
        if self._importing:
            # The import would write the playlist back right after deleting it.
            self._toast(_("Please wait for the import to finish."))
            return
        if self.player is not None and self.player.item is not None \
                and self.player.item.playlist_id == playlist_id:
            self.player.close()
        prov = self._providers.get(playlist_id)
        self.storage.delete_playlist(playlist_id)
        if prov is not None and not any(
                p.host == prov.host and p.username == prov.username
                for pid, p in self._providers.items() if pid != playlist_id):
            secrets.clear_password(prov.host, prov.username)
        self._load_providers()
        self._invalidate_pages()
        self._toast(_("Playlist removed."))
        if not self.storage.has_playlists():
            self._epg_guide = None
            self._epg_source = ""
            self.root_stack.set_visible_child_name("welcome")

    def _on_playlist_enabled(self, _w, playlist_id: int, enabled: bool) -> None:
        if not enabled and self.player is not None and self.player.item is not None \
                and self.player.item.playlist_id == playlist_id:
            self.player.close()
        self.storage.set_playlist_enabled(playlist_id, enabled)
        self._invalidate_pages()
        self._epg_source = ""
        self._autoload_epg(force_id=playlist_id if enabled else None)

    def _auto_refresh(self) -> bool:
        if self.settings.get("auto_refresh"):
            max_age = int(self.settings.get("refresh_hours")) * 3600
            self._refresh_queue = self.storage.stale_playlists(max_age)
            self._next_auto_refresh()
        return GLib.SOURCE_REMOVE

    def _next_auto_refresh(self) -> None:
        """Refresh the queued playlists one after another, quietly."""
        while self._refresh_queue:
            if self._importing:
                return  # a manual import is running; resumes when it ends
            if self._refresh_playlist(self._refresh_queue.pop(0), quiet=True):
                return

    # ---- automatic EPG -----------------------------------------------

    def _autoload_epg(self, force_id: int | None = None) -> None:
        rows = list(self.storage.list_playlists())
        if force_id is not None:
            rows.sort(key=lambda r: r["id"] != force_id)
        for row in rows:
            if not row["enabled"]:
                continue
            url = ""
            prov = self._providers.get(int(row["id"]))
            if prov is not None and prov.password:
                url = prov.xmltv_url()
            else:
                url = self.storage.playlist_meta(int(row["id"])).get("epg_url", "")
            if url:
                self._load_epg(url)
                return

    def _load_epg(self, url: str) -> None:
        if url == self._epg_source:
            return
        self._epg_source = url
        # Results of an older request (other playlist, deleted one…) are ignored.
        epg.fetch_guide_async(url, on_done=lambda g: self._on_epg_ready(g, url),
                              on_error=lambda e: self._on_epg_error(e, url))

    def _on_epg_error(self, exc, url: str) -> bool:
        if url == self._epg_source:
            self._epg_source = ""
            self._toast(_("Guide (EPG) unavailable: {error}").format(
                error=http.describe_error(exc)))
        return GLib.SOURCE_REMOVE

    def _on_epg_ready(self, guide, url: str) -> bool:
        if url != self._epg_source:
            return GLib.SOURCE_REMOVE
        self._epg_guide = guide
        for key in ("home", "channels", "favorites"):
            self.pages[key].set_guide(guide)
        if self.player is not None:
            self.player.set_guide(guide)
        return GLib.SOURCE_REMOVE

    # ---- preferences --------------------------------------------------

    def _connect_settings(self) -> None:
        s = self.settings
        self.storage.hide_adult = bool(s.get("hide_adult"))
        s.connect("hide_adult", self._on_hide_adult)
        self.storage.group_variants = bool(s.get("group_variants"))
        self.storage.hidden_groups = set(s.get("hidden_groups") or [])
        self.storage.group_order = list(s.get("group_order") or [])
        s.connect("hidden_groups", self._on_groups_setting)
        s.connect("group_order", self._on_groups_setting)
        s.connect("group_variants", self._on_group_variants)
        s.connect("show_logos", lambda _v: self._invalidate_pages(
            "home", "channels", "favorites", "movies", "series"))

    # ---- reminders ---------------------------------------------------

    def _on_remind(self, _page, channel: Channel, prog) -> None:
        reminder = Reminder(playlist_id=channel.playlist_id, channel_url=channel.url,
                            channel_name=channel.base_name if channel.variant_labels
                            else channel.display_name,
                            title=prog.title or _("Programme"), start=int(prog.start))
        is_set = self.reminders.toggle(reminder)
        self.settings.set("reminders", self.reminders.to_list())
        when = GLib.DateTime.new_from_unix_local(reminder.start).format("%R")
        self._toast(_("You will be reminded of “{title}” at {time}").format(
            title=reminder.title, time=when) if is_set else _("Reminder cancelled"))

    def _check_reminders(self) -> bool:
        now = time.time()
        due = self.reminders.due(now)
        if due or self.reminders.prune(now):
            self.settings.set("reminders", self.reminders.to_list())
        for r in due:
            self._announce(r)
        return GLib.SOURCE_CONTINUE

    def _announce(self, r: Reminder) -> None:
        text = _("“{title}” starts soon on {channel}").format(title=r.title,
                                                               channel=r.channel_name)
        notification = Gio.Notification.new(_("AshyTV reminder"))
        notification.set_body(text)
        notification.set_default_action_and_target("app.watch-reminder",
                                                   GLib.Variant("s", r.key))
        notification.add_button_with_target(_("Watch"), "app.watch-reminder",
                                            GLib.Variant("s", r.key))
        app = self.get_application()
        if app is not None:
            app.send_notification(f"reminder-{r.key}", notification)
        toast = Adw.Toast(title=GLib.markup_escape_text(text), timeout=10,
                          button_label=_("Watch"))
        toast.connect("button-clicked", lambda *_: self.watch_reminder(r.key))
        self.toasts.add_toast(toast)

    def watch_reminder(self, key: str) -> None:
        playlist_id, _sep, rest = key.partition("|")
        url = rest.rsplit("|", 1)[0]
        channel = self.storage.find_channel(int(playlist_id or 0), url)
        if channel is None:
            self._toast(_("This channel is no longer in your playlists."))
            return
        self.present()
        self._play_channel(channel)

    def _on_groups_setting(self, _value) -> None:
        self.storage.hidden_groups = set(self.settings.get("hidden_groups") or [])
        self.storage.group_order = list(self.settings.get("group_order") or [])
        self._invalidate_pages("home", "channels", "favorites")

    def _on_hide_group(self, _page, group: str) -> None:
        hidden = list(self.settings.get("hidden_groups") or [])
        if group not in hidden:
            self.settings.set("hidden_groups", [*hidden, group])
        toast = Adw.Toast(title=GLib.markup_escape_text(
            _("Group “{name}” hidden").format(name=group)), timeout=5,
            button_label=_("Undo"))
        toast.connect("button-clicked", lambda *_: self.settings.set(
            "hidden_groups", [g for g in self.settings.get("hidden_groups") if g != group]))
        self.toasts.add_toast(toast)

    def _on_move_group(self, _page, group: str, delta: int) -> None:
        order = [g for g, _n in self.storage.groups(MediaType.LIVE)]
        if group not in order:
            return
        i = order.index(group)
        j = max(0, min(len(order) - 1, i + delta))
        order.insert(j, order.pop(i))
        self.settings.set("group_order", order)

    def _on_group_variants(self, value) -> None:
        self.storage.group_variants = bool(value)
        self._invalidate_pages("home", "channels", "favorites")

    def _on_hide_adult(self, value) -> None:
        self.storage.hide_adult = bool(value)
        self._invalidate_pages()

    # ---- keyboard / utils ------------------------------------------------

    def _install_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        # Bubble phase: only keys no widget used (e.g. ← at the left edge).
        nav_keys = Gtk.EventControllerKey()
        nav_keys.connect("key-pressed", self._on_nav_key)
        self.add_controller(nav_keys)

    def _on_key(self, _c, keyval, _code, state) -> bool:
        if self.root_stack.get_visible_child_name() == "player" and self.player:
            if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
                return False
            return self.player.handle_key(keyval, state)
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F) and self.search_btn.get_visible():
            self.search_btn.set_active(True)
            self.search.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape and self.search_btn.get_active():
            self.search_btn.set_active(False)
            return True
        direction = self._DIRECTIONS.get(keyval)
        if direction is not None and not state & (Gdk.ModifierType.CONTROL_MASK
                                                  | Gdk.ModifierType.ALT_MASK
                                                  | Gdk.ModifierType.SHIFT_MASK):
            return self._move_focus(direction)
        return self._type_to_search(keyval, state)

    def _move_focus(self, direction: Gtk.DirectionType) -> bool:
        """Arrow keys: spatial navigation over the whole window (see keynav)."""
        focus = self.get_focus()
        if focus is None:
            return self.focus_page() or self.nav.focus_active() or True
        if direction == Gtk.DirectionType.RIGHT and self.nav.has_focus_within(focus) \
                and self.focus_page():
            return True  # leaving the sidebar enters the content
        if not focus.get_mapped():
            # The focused widget was hidden (emptied list, closed view…).
            return self.focus_page() or self.nav.focus_active() or True
        return keynav.move(self, direction, prefer={self.nav: self.nav.focus_active},
                           walls=(self.nav,))

    def _focus_is_editable(self) -> bool:
        focus = self.get_focus()
        return focus is not None and (isinstance(focus, Gtk.Editable)
                                      or focus.get_ancestor(Gtk.Editable) is not None)

    def _type_to_search(self, keyval: int, state) -> bool:
        """Typing on a searchable page starts searching right away."""
        if (state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                     | Gdk.ModifierType.SUPER_MASK)
                or self.root_stack.get_visible_child_name() != "shell"
                or not self.search_btn.get_visible() or self._focus_is_editable()):
            return False
        char = chr(Gdk.keyval_to_unicode(keyval) or 0)
        if not char.isprintable() or char.isspace() or char == "\0":
            return False
        if not self.search_btn.get_active():
            self.search_btn.set_active(True)
        self.search.grab_focus()
        self.search.insert_text(char, -1)
        self.search.set_position(-1)
        return True

    def _on_nav_key(self, _c, keyval, _code, state) -> bool:
        """Remote-style movement between the sidebar and the page."""
        if (self.root_stack.get_visible_child_name() != "shell"
                or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)):
            return False
        focus = self.get_focus()
        in_nav = self.nav.has_focus_within(focus)
        if not in_nav and keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Escape):
            if keyval == Gdk.KEY_BackSpace and self._focus_is_editable():
                return False
            if self.split.get_collapsed():
                self.split.set_show_sidebar(True)
            self.nav.focus_active()
            return True
        return False

    _DIRECTIONS = {Gdk.KEY_Up: Gtk.DirectionType.UP, Gdk.KEY_Down: Gtk.DirectionType.DOWN,
                   Gdk.KEY_Left: Gtk.DirectionType.LEFT,
                   Gdk.KEY_Right: Gtk.DirectionType.RIGHT}

    def handle_remote_key(self, keyval: int) -> bool:
        """A key from a game controller / remote: behave like the keyboard."""
        if not self.is_active():
            return False  # another application has the focus
        dialog = self.get_visible_dialog()
        if dialog is not None and keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            dialog.close()
            return True
        if self._on_key(None, keyval, 0, 0):
            return True
        focus = self.get_focus()
        direction = self._DIRECTIONS.get(keyval)
        if direction is not None:
            # Not handled above (e.g. the channel list over the video): a real
            # key would reach the focused list; move the focus like it would.
            return self.child_focus(direction) or True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_space) and focus is not None:
            return activate_widget(focus)
        return self._on_nav_key(None, keyval, 0, 0)

    def focus_page(self) -> bool:
        page = self.pages.get(self._current)
        if page is None:
            return False
        if hasattr(page, "focus_content") and page.focus_content():
            return True
        return page.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _on_close_request(self, _w) -> bool:
        if self.mini is not None:
            self._close_mini(stop=True)
        if self.player is not None:
            self.player.shutdown()
        return False

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))


def activate_widget(widget: Gtk.Widget) -> bool:
    """Activate the focused widget like Enter would.

    Buttons, list rows and list/grid items all bind Enter to their
    "activate" signal, so widget.activate() covers every case.
    """
    return widget.activate()
