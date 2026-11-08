"""AshyTV main window — shell with a side navigation."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import APP_ID
from .i18n import _, ngettext
from .models import Channel, Episode, MediaType, PlayItem, Variant
from .services import doh, epg, failover, free_tv, http, importer, secrets
from .services.quality import quality_label
from .services.gamepad import GamepadInput
from .services.recorder import Recording, safe_name
from .services import recordings as rec
from .services.recordings import RecordingBook, Scheduled
from .services.reminders import Reminder, ReminderBook
from .services.storage import Storage, collapse_variants
from .services.xtream_client import (
    XtreamProvider,
    normalize_host,
    series_details,
    vod_info,
)
from .ui import focus as focus_util
from .ui import keynav
from .ui.cast import ActorDialog, CastService
from .ui.detail_dialog import DetailDialog
from .ui.hidden_dialog import HiddenDialog
from .ui.nav_sidebar import NavRail, NavSidebar
from .ui.pages.catalog_page import CatalogPage
from .ui.pages.channels_page import ChannelsPage
from .ui.pages.favorites_page import FavoritesPage
from .ui.pages.search_page import SearchPage
from .ui.pages.home_page import HomePage
from .ui.pages.lists_page import ListsPage
from .ui.pages.recordings_page import RecordingsPage
from .ui.pages.settings_page import recordings_dir
from .ui.pages.settings_page import SettingsPage
from .ui.schedule_dialog import schedule_dialog
from .ui.tour import Step, Tour, mark_seen, should_offer
from .ui.welcome_view import WelcomeView

# Modifier keys alone don't start keyboard mode (Ctrl+click, Alt+Tab…).
_MODIFIER_KEYS = frozenset({
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock,
})
# A page switch crossfades this long; then its widgets can take the focus.
_PAGE_SWITCH_MS = 200
_IMPORT_BUSY = _("Please wait: an import is already in progress.")
_SEARCHABLE = {"channels", "movies", "series", "favorites"}
_EPISODE_CACHE_SECS = 6 * 3600

# URL schemes a playlist may hand to mpv.
_STREAM_SCHEMES = frozenset({
    "http", "https", "rtmp", "rtmps", "rtmpt", "rtmpe", "rtsp", "rtsps",
    "udp", "rtp", "srt", "mms", "mmsh",
})
# Anything else plays this instead, which fails with the usual error screen.
_BLOCKED_URL = "/nonexistent/ashytv-blocked-url"


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
        self._last_recorded = ""  # key of the item whose history was saved last
        self._passwords: dict[tuple[str, str], str] = {}  # keyring lookups
        self._current = "home"
        self._providers: dict[int, XtreamProvider] = {}
        self._zap: list | Gio.ListModel | None = None
        self._zap_index = 0
        self._prev_zap_index: int | None = None
        self._series_ctx: tuple[Channel, list[Episode]] | None = None
        self._importing = False
        self._quiet_id = 0
        self._edit_dialog = None  # the PlaylistDialog waiting for an edit's import
        self._focus_before_player: Gtk.Widget | None = None
        self._vod_info: dict[tuple[int, str], dict] = {}
        self._refresh_queue: list[int] = []
        self._fetched_series: dict[int, float] = {}
        self.cast = CastService(storage)
        self._servers_before: dict[int, int] = {}
        self._overlay_zap: list[Channel] | None = None
        self._epg_fallbacks: list[str] = []
        self._health_running: set[int] = set()

        self._load_providers()

        # ---- pages ----
        self.pages: dict[str, Gtk.Widget] = {}
        # Not homogeneous: the widest page (Channels with its guide) must not
        # force every page — and the window — to its minimum width, which
        # pushed chips and posters out of sight on narrow windows.
        self.page_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    transition_duration=150, hhomogeneous=False,
                                    vhomogeneous=False)
        self.page_stack.set_name(keynav.PAGES_NAME)
        # Before the pages: the guide and the Recordings page are handed this
        # as they are built.
        self.recordings = RecordingBook(self.settings.get("scheduled_recordings"))
        # The workers of the recordings running now, by key.
        self._recorders: dict[str, Recording] = {}
        self._build_pages()

        # ---- sidebar ----
        self.nav = NavSidebar()
        self.nav.connect("navigate", lambda _w, key: self._on_nav_navigate(key))

        # ---- header (spans the full width, as in the mockup) ----
        header = Adw.HeaderBar()
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
        menu.append(_("Guided tour"), "app.tour")
        menu.append(_("Keyboard Shortcuts"), "app.shortcuts")
        menu.append(_("About AshyTV"), "app.about")
        menu.append(_("Quit"), "app.quit")
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text=_("Main Menu"))
        header.pack_end(self.menu_btn)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text=_("Search (Ctrl+F)"))
        self.search_btn.connect("toggled", self._on_search_toggled)
        header.pack_end(self.search_btn)

        self.banner = Adw.Banner(revealed=False)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.banner)
        content.append(self.page_stack)
        self.page_stack.set_vexpand(True)

        # The menu: a rail of icons that opens over the page (as on the TVs).
        self.rail = NavRail(self.nav, content)

        shell = Adw.ToolbarView()
        shell.add_top_bar(header)
        shell.set_content(self.rail)

        # ---- welcome + shell + player (root stack) ----
        # The welcome screen is built the first time it is asked for. Almost
        # every launch has playlists already and never shows it, and building
        # it cost an eighth of a second of the time before the window
        # appeared.
        self.welcome: WelcomeView | None = None

        self.toasts = Adw.ToastOverlay()
        self.root_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.add_named(shell, "shell")
        self.toasts.set_child(self.root_stack)
        # The guided tour draws over everything (toasts included).
        self.tour_layer = Gtk.Overlay(child=self.toasts)
        self.tour: Tour | None = None
        self.set_content(self.tour_layer)

        self._install_keys()
        self.gamepad = GamepadInput(self.handle_remote_key)
        self.reminders = ReminderBook(self.settings.get("reminders"))
        if self.pages["channels"].guide is not None:
            self.pages["channels"].guide.reminders = self.reminders
            self.pages["channels"].guide.recordings = self.recordings
        GLib.timeout_add_seconds(20, self._check_reminders)
        GLib.timeout_add_seconds(20, self._check_recordings)
        # A recording running when the app was last closed is not running now.
        self._reset_recordings()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)
        self._connect_settings()

        if self.storage.has_playlists():
            self.root_stack.set_visible_child_name("shell")
            self.navigate("home")
            # The app opens with the focus on the banner (not on the menu, which would open).
            GLib.idle_add(lambda: self.focus_page() and False)
            self._autoload_epg()
            GLib.timeout_add_seconds(3, self._auto_refresh)
            GLib.timeout_add(1500, self._offer_tour)
            # Server status for the Playlists page (then every 15 minutes).
            GLib.timeout_add_seconds(5, self._check_once,
                                     [int(r["id"]) for r in storage.list_playlists()
                                      if r["enabled"]])
        else:
            self._show_welcome()

    def _show_welcome(self) -> None:
        """The welcome screen, built on first use (see __init__)."""
        if self.welcome is None:
            self.welcome = WelcomeView()
            self.welcome.connect("start", self._on_start)
            self.root_stack.add_named(self.welcome, "welcome")
        self.root_stack.set_visible_child_name("welcome")

    # ---- page construction ----------------------------------------

    def _build_pages(self) -> None:
        home = HomePage(self.storage)
        home.connect("play", self._on_play)
        home.connect("resume", self._on_resume)
        home.connect("navigate", lambda _w, k: self.navigate(k))
        home.connect("watch", lambda _w, ch: self._play_channel(ch))
        home.connect("open-group", self._on_home_group)
        home.backdrop_lookup = self.cast.load_backdrop

        channels = ChannelsPage(self.storage, MediaType.LIVE)
        channels.connect("play", self._on_play)
        channels.connect("favorite-changed", self._on_favorite_changed)
        channels.connect("show-schedule", self._on_show_schedule)
        channels.connect("hide-group", lambda _p, group: self._hide_group(MediaType.LIVE, group))
        channels.connect("hide-channel", self._on_hide_channel)
        channels.connect("show-hidden", lambda *_: self.show_hidden())
        channels.connect("remind", self._on_remind)
        channels.connect("record", self._on_record)
        channels.connect("move-group", self._on_move_group)
        channels.preview_source = self._preview_source
        if channels.info is not None:
            channels.info.configure(bool(self.settings.get("hwdec")),
                                    float(self.settings.get("volume")))

        movies = CatalogPage(self.storage, MediaType.MOVIE, _("Movies"),
                             _("The best movies for you"))
        movies.connect("play", self._on_play)
        movies.connect("favorite-changed", self._on_favorite_changed)
        movies.connect("hide-group", lambda _p, mt, group: self._hide_group(mt, group))
        movies.connect("show-hidden", lambda *_: self.show_hidden())
        movies.connect("show-person", self._on_search_person)
        movies.people_search = self.cast.search_people

        series = CatalogPage(self.storage, MediaType.SERIES, _("Series"),
                             _("Your favorite series in one place"))
        series.connect("play", self._on_play)
        series.connect("favorite-changed", self._on_favorite_changed)
        series.connect("hide-group", lambda _p, mt, group: self._hide_group(mt, group))
        series.connect("show-hidden", lambda *_: self.show_hidden())
        series.connect("show-person", self._on_search_person)
        series.people_search = self.cast.search_people

        # Favorites: channels, movies, series and people, one rail each.
        favorites = FavoritesPage(self.storage)
        favorites.connect("play", self._on_play)
        favorites.connect("person", self._on_search_person)

        # Search from the pages without a list of their own (🔍 / typing).
        search = SearchPage(self.storage)
        search.people_search = self.cast.search_people
        search.connect("play", self._on_play)
        search.connect("person", self._on_search_person)

        recordings = RecordingsPage()
        recordings.recordings = self.recordings
        recordings.connect("cancel-scheduled", self._on_cancel_scheduled)
        recordings.connect("play", self._on_play)

        lists = ListsPage(self.storage)
        lists.connect("import-requested", self._on_import_requested)
        lists.connect("xtream-requested", self._on_xtream_requested)
        lists.connect("refresh-requested", lambda _w, pid: self._refresh_playlist(pid))
        lists.connect("delete-requested", self._on_delete_requested)
        lists.connect("enabled-changed", self._on_playlist_enabled)
        lists.connect("servers-changed", self._on_servers_changed)
        lists.connect("check-servers", lambda _w, pid: self._check_servers([pid]))
        lists.connect("edit-requested", self._on_playlist_edited)
        lists.provider_lookup = self._providers.get
        lists.connect("guide-link-changed", self._on_guide_link)

        settings = SettingsPage(self.settings, self.storage)
        settings.connect("toast", lambda _w, msg: self._toast(msg))
        settings.connect("history-cleared", lambda *_: self.pages["home"].invalidate())
        settings.connect("show-hidden", lambda *_: self.show_hidden())

        self.pages = {
            "home": home, "channels": channels,
            "movies": movies, "series": series, "favorites": favorites,
            "recordings": recordings, "lists": lists, "settings": settings,
            "search": search,
        }
        for key, page in self.pages.items():
            self.page_stack.add_named(page, key)

    # ---- navigation -----------------------------------------------------

    def navigate(self, key: str) -> None:
        if key not in self.pages:
            return
        self._current = key
        if key != "search":  # not a menu item: the page searched from stays marked
            self.nav.set_active(key)
        self.page_stack.set_visible_child_name(key)
        page = self.pages[key]
        if hasattr(page, "refresh"):
            page.refresh()
        # The 🔍 is on every page: it filters the pages with a list and, on the
        # others, opens the search of everything (the "search" page).
        searchable = key in _SEARCHABLE or key == "search"
        if not searchable:
            self.search_btn.set_active(False)
        elif hasattr(page, "set_search"):
            # The new page filters by the text already in the search box.
            page.set_search(self.search.get_text())
        if getattr(self, "keyboard", None) is not None and self.keyboard.active:
            GLib.timeout_add(_PAGE_SWITCH_MS + 50, self._keep_focus_on_screen)

    def _on_home_group(self, _home, media_type: str, chip: str) -> None:
        """Home's "See more": Movies or Series on that group."""
        key = "series" if media_type == MediaType.SERIES.value else "movies"
        self.pages[key].open_chip(chip)
        self.navigate(key)
        if self.keyboard.active:
            GLib.timeout_add(_PAGE_SWITCH_MS, self._enter_page)

    def _on_nav_navigate(self, key: str) -> None:
        self.navigate(key)
        if self.keyboard.active:
            # Enter on a sidebar item: the focus goes into the page opened.
            GLib.timeout_add(_PAGE_SWITCH_MS, self._enter_page)

    def _keep_focus_on_screen(self) -> bool:
        """After a page switch by keyboard: if the focused widget went away
        with the old page (e.g. a "See all" button), land on the new one."""
        focus = self.get_focus()
        page = self.pages.get(self._current)
        on_page = focus is not None and page is not None and focus.is_ancestor(page)
        elsewhere = focus is not None and not focus.is_ancestor(self.page_stack)
        if not focus_util.is_visible(focus) or not (on_page or elsewhere):
            self._enter_page()
        return GLib.SOURCE_REMOVE

    def _on_start(self, _w) -> None:
        self.root_stack.set_visible_child_name("shell")
        self.navigate("lists" if not self.storage.has_playlists() else "home")
        GLib.idle_add(lambda: self.focus_page() and False)

    def _on_search_toggled(self, button) -> None:
        active = button.get_active()
        self.search.set_visible(active)
        if active:
            if self._current not in _SEARCHABLE and self._current != "search":
                self._search_return = self._current
                self.navigate("search")
            self.search.grab_focus()
        else:
            self.search.set_text("")
            if self._current == "search":
                # Closing the search goes back to the page it was opened from.
                self.navigate(getattr(self, "_search_return", "home") or "home")
                if self.keyboard.active:
                    GLib.timeout_add(_PAGE_SWITCH_MS, self._enter_page)
            elif self.keyboard.active:
                # Esc on a page's own search: back into the page (not the 🔍).
                GLib.idle_add(self._enter_page)

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
                    # The keyring answers over D-Bus, synchronously: ask once
                    # per account, not on every import.
                    key = (prov.host, prov.username)
                    if key not in self._passwords:
                        self._passwords[key] = secrets.lookup_password(*key) or ""
                    prov.password = self._passwords[key]
                prov.backups = failover.clean_backups(self.storage.backups(row["id"]),
                                                      prov.host)
                prov.active = row["active_server"] or 0
                if not 0 <= prov.active < len(prov.servers):
                    prov.active = 0
                self._providers[int(row["id"])] = prov
            except (ValueError, KeyError):
                continue

    def _resolve(self, url: str, playlist_id: int) -> str:
        """Relative Xtream provider path -> URL with credentials.

        A playlist is untrusted input: only network streaming protocols (and
        local paths) reach mpv. Its own protocols (edl://, av://, lavfi…)
        could do much more than play a stream.
        """
        if "://" in url:
            return url if url.split("://", 1)[0].lower() in _STREAM_SCHEMES else _BLOCKED_URL
        if url.startswith("/"):
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
            user_agent=channel.user_agent, referrer=channel.referrer,
            sub_files=self._vod_info.get((channel.playlist_id, channel.ext_id), {})
            .get("subtitles", []))
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
        # One stream at a time: providers often allow a single connection.
        self.pages["channels"].stop_preview()
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
        self.player.connect("groups-requested", self._on_groups_requested)
        self.player.channel_overlay.connect("group-selected", self._on_overlay_group)
        self.player.channel_overlay.connect("notify::visible", self._on_overlay_visible)
        self.player.connect("zap-to", lambda _p, index: self._zap_to(index))
        self.player.connect("zap-previous", lambda *_: self._zap_previous())
        self.player.connect("toast", lambda _p, msg: self._toast(msg))
        self.player.server_failover = self._stream_failover
        self.player.stream_target = doh.stream_target
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
        if self.root_stack.get_visible_child_name() != "player" or self.player is None:
            return  # a number typed just before closing the player
        if self._overlay_zap is not None:
            # Chosen from another group in the channel list: zap through it.
            self._zap, self._overlay_zap = self._overlay_zap, None
            self._zap_index = -1
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
        self._overlay_zap = None
        n = self._zap_len()
        if n:
            channels = [self._zap_item(i) for i in range(n)]
            self.player.show_channel_list(channels, self._zap_index, self._epg_guide,
                                          self._zap_title())

    def _zap_title(self) -> str:
        """Title of the channel list over the video: the group being zapped."""
        if self._zap is self.pages["channels"].filter_model:
            row = self.pages["channels"].group_list.get_selected_row()
            label = row.get_child().get_first_child().get_label() if row else ""
            if label:
                return label
        return _("Channels")

    # Keys of the groups column besides the group names (no NUL: they go
    # through a GObject string signal).
    _FAV_GROUP, _RECENT_GROUP = "\x01fav", "\x01recent"

    def _live_groups(self) -> list[tuple[str, str, int]]:
        """Favorites, Recent and every visible live group, with counts."""
        grouped = self.storage.group_variants
        fav = self.storage.count(MediaType.LIVE, favorites=True, collapse=grouped)
        recent = len(self._group_channels(self._RECENT_GROUP))
        return ([(self._FAV_GROUP, _("Favorites"), fav),
                 (self._RECENT_GROUP, _("Recently watched"), recent)]
                + [(g, g, n) for g, n in self.storage.groups(MediaType.LIVE, collapse=grouped)])

    def _group_channels(self, key: str) -> list[Channel]:
        if key == self._FAV_GROUP:
            chans = self.storage.channels(MediaType.LIVE, favorites=True)
        elif key == self._RECENT_GROUP:
            chans = self.storage.recent_channels(MediaType.LIVE)
        else:
            chans = self.storage.channels(MediaType.LIVE, group=key)
        return collapse_variants(chans) if self.storage.group_variants else chans

    def _on_groups_requested(self, _p) -> None:
        """← again in the channel list: the groups column, on the group of
        the channel being watched."""
        item = self.player.item
        current = item.channel.group if item is not None and item.channel else ""
        self.player.channel_overlay.show_groups(self._live_groups(), current)

    def _on_overlay_group(self, overlay, key: str) -> None:
        chans = self._group_channels(key)
        self._overlay_zap = chans  # zapping follows this group once one is chosen
        label = {self._FAV_GROUP: _("Favorites"),
                 self._RECENT_GROUP: _("Recently watched")}.get(key, key)
        overlay.set_channels(chans, label)

    def _on_overlay_visible(self, overlay, _p) -> None:
        if not overlay.get_visible():
            self._overlay_zap = None  # closed without choosing: keep zapping as before

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
        try:
            self.storage.record_history(
                item.playlist_id, item.key, item.media_type, item.title,
                subtitle=item.subtitle, logo=item.logo, tvg_id=item.tvg_id,
                position=pos, duration=dur,
                parent=item.channel.url if item.media_type is MediaType.SERIES
                and item.channel else "")
        except sqlite3.Error:
            return  # busy with an import: the next save (10 s later) catches up
        # Progress saves of the same item (every 10 s) only mark the Home as
        # stale: rebuilding it while visible (picture-in-picture) reset its
        # scroll and keyboard focus each time.
        home = self.pages["home"]
        if item.key != self._last_recorded:
            self._last_recorded = item.key
            home.invalidate()
        else:
            home.mark_stale()

    def _on_resume(self, _page, row) -> None:
        mt = MediaType(row["media_type"])
        pid = row["playlist_id"]
        if mt is MediaType.SERIES and row["parent"]:
            series = self.storage.find_channel(pid, row["parent"])
            if series is None:
                self._toast(_("This item is no longer in the playlist."))
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
        if ep is None:
            self._toast(_("This episode is no longer available."))
            return
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
        if widget is not None and widget.get_mapped() and widget.grab_focus():
            return GLib.SOURCE_REMOVE
        # The page was rebuilt (Home's "Continue watching" changed): the card
        # of the same channel/title in the new one.
        key = getattr(widget, "ashy_key", None)
        twin = self._find_card(self.pages.get(self._current), key) if key else None
        if twin is not None and twin.grab_focus():
            focus_util.reveal(twin)
            return GLib.SOURCE_REMOVE
        self.focus_page() or self.nav.focus_active()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _find_card(root: Gtk.Widget | None, key) -> Gtk.Widget | None:
        stack = [root] if root is not None else []
        while stack:
            node = stack.pop(0)
            if getattr(node, "ashy_key", None) == key and node.get_mapped():
                return node
            child = node.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return None

    def _on_player_fav(self, _p, item: PlayItem) -> None:
        ch = item.channel
        if not self.storage.set_favorite(ch, not ch.favorite):
            self._toast(_busy_message())
            return
        self._on_favorite_changed(None, ch)
        self._toast(_("Added to favorites") if ch.favorite else _("Removed from favorites"))

    def _on_favorite_changed(self, source, channel) -> None:
        # The page the user is on (or is under the player / a dialog) is
        # updated in place, keeping its scroll and focus; the others reload
        # when shown.
        for key in ("home", "channels", "favorites", "movies", "series"):
            page = self.pages[key]
            if page is source:
                continue
            if key == self._current or page.get_mapped():
                page.favorite_changed(channel)
            else:
                page.invalidate()

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

    def _preview_source(self, channel: Channel) -> tuple[str, str, str] | None:
        """URL, user agent and referrer of the channel list's live preview."""
        if not self.settings.get("channel_preview") or self.mini is not None:
            return None  # off, or the mini player already uses the connection
        url = self._resolve(channel.url, channel.playlist_id)
        if url == _BLOCKED_URL:
            return None
        return url, channel.user_agent, channel.referrer

    def _open_detail(self, channel: Channel) -> None:
        dialog = DetailDialog(self.storage, channel, self._with_episodes,
                              self._with_vod_info, self.cast.load_cast)
        dialog.connect("play", self._on_detail_play)
        dialog.connect("favorite-changed", self._on_favorite_changed)
        dialog.connect("show-person", self._on_show_person)
        dialog.connect("open-title", lambda d, ch: (d.close(), self._open_detail(ch)))
        dialog.present(self)

    def _actor_dialog(self, member) -> ActorDialog:
        actor = ActorDialog(member, self.storage)
        actor.connect("favorite-changed", self._on_person_favorite)
        return actor

    def _on_person_favorite(self, _actor, member) -> None:
        followed = self.storage.is_favorite_person(member)
        self._toast(_("Added to favorites") if followed else _("Removed from favorites"))
        self.pages["favorites"].invalidate()

    def _on_show_person(self, detail: DetailDialog, member) -> None:
        actor = self._actor_dialog(member)
        actor.connect("open", self._on_actor_title, detail)
        actor.present(self)
        self.cast.load_person(member, actor.show_person)

    def _on_search_person(self, _page, member) -> None:
        """A person found by the search on Movies or Series (or a favorite one)."""
        actor = self._actor_dialog(member)
        actor.connect("open", lambda a, ch: (a.close(), self._open_detail(ch)))
        actor.present(self)
        self.cast.load_person(member, actor.show_person)

    def _on_actor_title(self, actor: ActorDialog, channel: Channel,
                        detail: DetailDialog) -> None:
        actor.close()
        same = (channel.playlist_id, channel.url) == (detail.channel.playlist_id,
                                                      detail.channel.url)
        if not same:
            detail.close()
            self._open_detail(channel)

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
                eps, details = series_details(prov, series.ext_id)
            except Exception as exc:
                GLib.idle_add(self._episodes_done, series, None, exc, on_done, on_error)
                return
            GLib.idle_add(self.cast.remember, series, details.get("cast", ""),
                          details.get("tmdb_id", ""))
            GLib.idle_add(self._episodes_done, series, eps, None, on_done, on_error)

        threading.Thread(target=worker, daemon=True).start()

    def _with_vod_info(self, movie: Channel, on_done, on_error) -> None:
        """Movie details from the Xtream API (synopsis, cast…), cached per run."""
        key = (movie.playlist_id, movie.ext_id)
        if key in self._vod_info:
            on_done(self._vod_info[key])
            return
        prov = self._providers.get(movie.playlist_id)
        if prov is None or not movie.ext_id or movie.media_type is not MediaType.MOVIE:
            on_done({})
            return

        def worker():
            try:
                info = vod_info(prov, movie.ext_id)
            except Exception as exc:
                GLib.idle_add(lambda e=exc: on_error(e) and False)
                return

            def done():
                self._sync_active(movie.playlist_id)
                self._vod_info[key] = info
                self.cast.remember(movie, info.get("cast", ""), info.get("tmdb_id", ""))
                on_done(info)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    def _episodes_done(self, series, eps, exc, on_done, on_error) -> bool:
        if exc is not None:
            cached = self.storage.episodes(series.id)
            if cached:
                on_done(cached)
            else:
                on_error(exc)
            return GLib.SOURCE_REMOVE
        self._sync_active(series.playlist_id)
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
                self._toast(_IMPORT_BUSY)
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

    def _on_import_requested(self, _w, name: str, source: str, backups,
                             guide_link: str) -> None:
        if not self._begin_import(_("Importing playlist…")):
            return
        db_path = self.storage.path
        backups = failover.clean_backups(backups, source) if "://" in source else None
        importer.run_async(
            lambda progress: importer.import_m3u(db_path, name, source, progress,
                                                 backups=backups,
                                                 epg_override=guide_link or None),
            self._import_done, self._import_error, self._import_progress)

    def _on_xtream_requested(self, _w, name: str, host: str, user: str, pwd: str,
                             backups) -> None:
        host = normalize_host(host)
        provider = XtreamProvider(host=host, username=user, password=pwd,
                                  backups=failover.clean_backups(backups, host))
        self._run_xtream_import(name, provider)

    def _run_xtream_import(self, name: str, provider: XtreamProvider,
                           quiet_id: int = 0, playlist_id: int = 0) -> bool:
        if not self._begin_import(_("Connecting to the Xtream provider…"), quiet_id):
            return False
        self._servers_before = self._server_snapshot()
        db_path = self.storage.path
        importer.run_async(
            lambda progress: importer.import_xtream(db_path, name, provider, progress,
                                                    playlist_id=playlist_id),
            self._import_done, self._import_error, self._import_progress)
        return True

    def _on_playlist_edited(self, _w, playlist_id: int, v: dict, dialog) -> None:
        """Saved in the "Edit playlist" dialog. A new name, backups or guide
        link apply at once; a new address or login loads the playlist again
        (in the same place: favorites and history stay)."""
        row = self.storage.get_playlist(playlist_id)
        if row is None:
            dialog.finish(None)
            return
        name = v["name"] or row["name"]
        if row["kind"] == "xtream":
            error = self._edit_xtream(playlist_id, name, v, dialog)
        elif row["kind"] == "m3u" and "://" in row["source"] \
                and not free_tv.is_free_tv(row["source"]):
            error = self._edit_m3u_url(playlist_id, row["source"], name, v, dialog)
        else:
            free = free_tv.is_free_tv(row["source"])
            self._apply_edit(playlist_id, name, guide=None if free else v["guide"])
            error = None
        if error != "":  # "" = an import is running and answers the dialog later
            dialog.finish(error)

    def _source_taken(self, playlist_id: int, kind: str, key: str) -> bool:
        """Another playlist already has this source (Xtream: server + user)."""
        for r in self.storage.list_playlists():
            if r["kind"] != kind or int(r["id"]) == playlist_id:
                continue
            other = XtreamProvider.source_key(r["source"]) if kind == "xtream" else r["source"]
            if other == key:
                return True
        return False

    def _edit_xtream(self, playlist_id: int, name: str, v: dict, dialog) -> str | None:
        old = self._providers.get(playlist_id)
        host = normalize_host(v["host"])
        backups = failover.clean_backups(v["backups"], host)
        if old is not None and (host, v["username"], v["password"]) == \
                (old.host, old.username, old.password):
            self._apply_edit(playlist_id, name, backups if backups != old.backups else None)
            return None
        key = XtreamProvider.source_key(XtreamProvider(host, v["username"], "").to_json())
        if self._source_taken(playlist_id, "xtream", key):
            return _("Another playlist already uses this server and user.")
        provider = XtreamProvider(host=host, username=v["username"],
                                  password=v["password"], backups=backups)
        return self._start_edit_import(dialog, lambda: self._run_xtream_import(
            name, provider, playlist_id=playlist_id))

    def _edit_m3u_url(self, playlist_id: int, source: str, name: str, v: dict,
                      dialog) -> str | None:
        url = v["url"]
        backups = failover.clean_backups(v["backups"], url)
        if url == source:
            old = failover.clean_backups(self.storage.backups(playlist_id), url)
            self._apply_edit(playlist_id, name, backups if backups != old else None,
                             v["guide"])
            return None
        if self._source_taken(playlist_id, "m3u", url):
            return _("Another playlist already uses this address.")
        if self._importing:
            return _IMPORT_BUSY
        self.storage.update_playlist_meta(playlist_id, epg_override=v["guide"])
        return self._start_edit_import(dialog, lambda: self._run_m3u_edit(
            playlist_id, name, url, backups))

    def _apply_edit(self, playlist_id: int, name: str, backups: list[str] | None = None,
                    guide: str | None = None) -> None:
        """What an edit changes without loading the playlist again."""
        self.storage.rename_playlist(playlist_id, name)
        if backups is not None:
            self._on_servers_changed(None, playlist_id, backups)
        if guide is not None and guide != self.storage.playlist_meta(playlist_id).get(
                "epg_override", ""):
            self._on_guide_link(None, playlist_id, guide)
        self._invalidate_pages()

    def _start_edit_import(self, dialog, start) -> str | None:
        """"" when the import started (it answers the dialog when done)."""
        self._edit_dialog = dialog
        if start():
            return ""
        self._edit_dialog = None
        return _IMPORT_BUSY

    def _run_m3u_edit(self, playlist_id: int, name: str, url: str,
                      backups: list[str]) -> bool:
        if not self._begin_import(_("Updating playlist…")):
            return False
        self._servers_before = self._server_snapshot()
        db_path = self.storage.path
        importer.run_async(
            lambda progress: importer.import_m3u(db_path, name, url, progress,
                                                 backups=backups, playlist_id=playlist_id),
            self._import_done, self._import_error, self._import_progress)
        return True

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
            backups, active = self.storage.backups(playlist_id), row["active_server"] or 0
            self._servers_before = self._server_snapshot()
            importer.run_async(
                lambda progress: importer.import_m3u(db_path, name, source, progress,
                                                     backups=backups, active=active),
                self._import_done, self._import_error, self._import_progress)
        return True

    def _import_done(self, result) -> None:
        quiet_id, self._quiet_id = self._quiet_id, 0
        edit, self._edit_dialog = self._edit_dialog, None
        self._importing = False
        self.banner.set_revealed(False)
        self._passwords.clear()  # the import may have saved a new password
        self._load_providers()
        before = self._servers_before
        before.setdefault(result.playlist_id, 0)  # a new list: did its main server fail?
        self._announce_server_changes(before)
        self._servers_before = {}
        self._invalidate_pages()
        GLib.timeout_add_seconds(2, self._check_once, [result.playlist_id])
        self._epg_source = ""
        self._autoload_epg(force_id=result.playlist_id)
        if quiet_id:
            self.pages["lists"].set_busy(False)
            row = self.storage.get_playlist(quiet_id)
            if row is not None:
                self._toast(_("Playlist “{name}” updated.").format(name=row["name"]))
            self._next_auto_refresh()
            return
        if edit is not None:
            # An edited playlist: stay on the Playlists page.
            self.pages["lists"].set_busy(False)
            edit.finish(None)
            row = self.storage.get_playlist(result.playlist_id)
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
        GLib.timeout_add(1500, self._offer_tour)  # right after the first playlist

    def _import_error(self, exc: Exception) -> None:
        quiet_id, self._quiet_id = self._quiet_id, 0
        edit, self._edit_dialog = self._edit_dialog, None
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
        if edit is not None:
            # The playlist keeps its old address and login.
            edit.finish(_("Failed: {error}").format(error=msg))
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
        try:
            self.storage.delete_playlist(playlist_id)
        except sqlite3.Error:
            self._toast(_busy_message())
            return
        if prov is not None:
            self._passwords.pop((prov.host, prov.username), None)
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
            self._show_welcome()

    def _on_playlist_enabled(self, _w, playlist_id: int, enabled: bool) -> None:
        if not enabled and self.player is not None and self.player.item is not None \
                and self.player.item.playlist_id == playlist_id:
            self.player.close()
        self.storage.set_playlist_enabled(playlist_id, enabled)
        # Not the Playlists page: it rebuilds itself once, after the switch.
        self._invalidate_pages(*(k for k in self.pages if k != "lists"))
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
            url, others = "", []
            prov = self._providers.get(int(row["id"]))
            if prov is not None and prov.password:
                url = prov.xmltv_url()
                # The guide from a backup server when the one in use fails.
                others = [prov.at(srv).xmltv_url() for i, srv in enumerate(prov.servers)
                          if i != prov.active]
            else:
                # The guide link set on the playlist wins over its url-tvg.
                meta = self.storage.playlist_meta(int(row["id"]))
                url = meta.get("epg_override") or meta.get("epg_url", "")
            if url:
                self._load_epg(url, others)
                return

    def _load_epg(self, url: str, fallbacks: list[str] | None = None) -> None:
        if url == self._epg_source:
            return
        self._epg_source = url
        self._epg_fallbacks = list(fallbacks or [])
        # Results of an older request (other playlist, deleted one…) are ignored.
        try:
            channels = self.storage.tvg_ids() or None
            names = self.storage.live_names()
        except sqlite3.Error:
            channels, names = None, None
        epg.fetch_guide_async(url, on_done=lambda g: self._on_epg_ready(g, url),
                              on_error=lambda e: self._on_epg_error(e, url),
                              channels=channels, names=names)

    def _on_epg_error(self, exc, url: str) -> bool:
        if url == self._epg_source and self._epg_fallbacks and failover.is_server_failure(exc):
            self._epg_source = ""
            nxt = self._epg_fallbacks.pop(0)
            self._load_epg(nxt, self._epg_fallbacks)
            return GLib.SOURCE_REMOVE
        if url == self._epg_source:
            self._epg_source = ""
            self._toast(_("Guide (EPG) unavailable: {error}").format(
                error=http.describe_error(exc)))
        return GLib.SOURCE_REMOVE

    def _on_epg_ready(self, guide, url: str) -> bool:
        if url != self._epg_source:
            return GLib.SOURCE_REMOVE
        self._epg_guide = guide
        try:
            # Channels linked to the guide by name get the guide's id.
            relinked = self.storage.relink_guide(guide.links)
            for pid, coverage in self.storage.guide_coverage(guide.has_channel).items():
                self.storage.update_playlist_meta(pid, epg_coverage=list(coverage))
        except sqlite3.Error:
            relinked = 0
        if relinked:
            self._invalidate_pages("channels", "favorites", "home")
        for key in ("home", "channels", "favorites"):
            self.pages[key].set_guide(guide)
        self.pages["lists"].invalidate()
        if self.player is not None:
            self.player.set_guide(guide)
        return GLib.SOURCE_REMOVE

    def _on_guide_link(self, _w, playlist_id: int, url: str) -> None:
        """The guide link of an M3U playlist changed: load that guide now."""
        self.storage.update_playlist_meta(playlist_id, epg_override=url)
        self.pages["lists"].invalidate()
        self._epg_source = ""
        self._autoload_epg(force_id=playlist_id)

    # ---- preferences --------------------------------------------------

    def _connect_settings(self) -> None:
        s = self.settings
        self.storage.hide_adult = bool(s.get("hide_adult"))
        s.connect("hide_adult", self._on_hide_adult)
        self.storage.group_variants = bool(s.get("group_variants"))
        self.storage.sort_az = bool(s.get("sort_az"))
        s.connect("sort_az", self._on_sort_az)
        if s.get("hidden_groups"):
            # Older versions kept one global list of group names: now they
            # are hidden per playlist in the database.
            self.storage.migrate_hidden_groups(s.get("hidden_groups"))
            s.set("hidden_groups", [])
        self.storage.group_order = list(s.get("group_order") or [])
        s.connect("group_order", self._on_groups_setting)
        s.connect("group_variants", self._on_group_variants)
        s.connect("show_logos", lambda _v: self._invalidate_pages(
            "home", "channels", "favorites", "movies", "series"))
        s.connect("channel_preview", lambda _v: self.pages["channels"].restart_preview())
        doh.configure(s.get("doh"))
        s.connect("doh", doh.configure)
        GLib.timeout_add_seconds(failover.CHECK_EVERY, self._periodic_check)

    # ---- guided tour ----------------------------------------------------------

    def _offer_tour(self) -> bool:
        if (not should_offer(self.settings, self.storage.has_playlists())
                or self.root_stack.get_visible_child_name() != "shell"
                or self.get_visible_dialog() is not None):
            return GLib.SOURCE_REMOVE
        mark_seen(self.settings)  # asked once, whatever the answer
        dialog = Adw.AlertDialog(
            heading=_("Would you like a quick tour of AshyTV?"),
            body=_("A few steps over the real screens: channels, guide, movies, "
                   "playlists and settings. You can reopen it from the main menu."))
        dialog.add_response("later", _("Not now"))
        dialog.add_response("start", _("Start"))
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")
        dialog.connect("response", self._on_tour_answer)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _on_tour_answer(self, _dialog, response: str) -> None:
        if response == "start":
            self.start_tour()

    def start_tour(self) -> None:
        if self.tour is not None and self.tour.active:
            return
        if self.root_stack.get_visible_child_name() == "player" and self.player:
            self.player.close()
        if self.root_stack.get_visible_child_name() != "shell":
            return
        dialog = self.get_visible_dialog()
        if dialog is not None:
            dialog.force_close()
        start_page = self._current
        mark_seen(self.settings)
        self.tour = Tour(self.tour_layer, self._tour_steps(), self.navigate,
                         lambda: self._tour_finished(start_page))
        self.tour.start()

    def _tour_finished(self, page: str) -> None:
        self.navigate(page)
        # The card's buttons are gone: the focus goes back to the page.
        GLib.timeout_add(250, self._refocus_page)

    def _refocus_page(self) -> bool:
        if not self.focus_page():
            self.nav.focus_active()
        return GLib.SOURCE_REMOVE

    def _tour_steps(self) -> list[Step]:
        ch, nav = self.pages["channels"], self.nav
        movies, lists, settings = self.pages["movies"], self.pages["lists"], self.pages["settings"]

        def channels_list():
            ch.set_view("list")

        def guide_buttons():
            return ch._view_buttons["guide"].get_parent() if ch.guide is not None else None

        return [
            Step(_("Sections"),
                 _("Home, TV channels, movies, series, favorites, recordings, playlists "
                   "and settings. The keyboard, a remote or a game controller work too."),
                 "home", lambda: nav),
            Step(_("Channel groups"),
                 _("Pick a group to see its channels. Right-click (or long-press) a group "
                   "to move it or hide it."),
                 "channels", lambda: ch.show_groups(), channels_list),
            Step(_("Channel list"),
                 _("A click selects a channel; a double-click or Enter opens it. "
                   "Right-click a channel to hide it or add it to your favorites."),
                 None, lambda: ch.list_panel),
            Step(_("Live preview"),
                 _("The selected channel plays here, muted (the button turns the sound "
                   "on), with what is on now and next."),
                 None, lambda: ch.info),
            Step(_("Hidden items"),
                 _("Everything you hid is here. Open it to show items again."),
                 None, lambda: (ch.show_groups(), ch.hidden_row())[1]),
            Step(_("Program guide"),
                 _("Switch to Guide to see what is on, hour by hour, and set reminders."),
                 None, guide_buttons),
            Step(_("Movies and series"),
                 _("Posters with long titles scroll when you point at them. Open one to "
                   "see the details and the cast; click an actor to see their other "
                   "titles in your playlists."),
                 "movies", lambda: movies.stack),
            Step(_("Favorites and recordings"),
                 _("The heart keeps channels, movies and series here; recordings of live "
                   "TV are listed in Recordings."),
                 None, lambda: nav._buttons.get("favorites")),
            Step(_("Playlists"),
                 _("Add M3U or Xtream lists. Each list can have backup servers (other "
                   "DNS of the provider): the dots show if each one is online, slow or "
                   "down, and AshyTV switches automatically."),
                 "lists", lambda: lists.existing_box if lists.existing_box.get_visible()
                 else lists.save_btn),
            Step(_("Settings"),
                 _("Themes and colors, hidden channels and groups, secure DNS, parental "
                   "PIN and more."),
                 "settings", lambda: settings.cats),
            Step(_("See it again"),
                 _("Reopen this tour from the main menu (Guided tour) or from "
                   "Settings → About."),
                 None, lambda: self.menu_btn),
        ]

    # ---- backup servers ----------------------------------------------------

    def _servers_of(self, playlist_id: int) -> list[str]:
        """Main + backup URLs of a playlist ([] when it has no web source)."""
        prov = self._providers.get(playlist_id)
        if prov is not None:
            return prov.servers
        row = self.storage.get_playlist(playlist_id)
        if row is None or row["kind"] == "xtream" or "://" not in row["source"]:
            return []
        return [row["source"], *failover.clean_backups(self.storage.backups(playlist_id),
                                                      row["source"])]

    def _sync_active(self, playlist_id: int) -> None:
        """A background request moved to another server: keep it (sticky)."""
        prov = self._providers.get(playlist_id)
        if prov is not None and prov.active != self.storage.active_server(playlist_id):
            self._set_active_server(playlist_id, prov.active)

    def _server_snapshot(self) -> dict[int, int]:
        return {int(r["id"]): int(r["active_server"] or 0)
                for r in self.storage.list_playlists()}

    def _announce_server_changes(self, before: dict[int, int]) -> None:
        """Requests in the background moved a playlist to another server."""
        for pid, old in before.items():
            row = self.storage.get_playlist(pid)
            if row is not None and int(row["active_server"] or 0) != old:
                self._server_switched(pid, old, int(row["active_server"] or 0))

    def _set_active_server(self, playlist_id: int, index: int) -> None:
        old = self.storage.active_server(playlist_id)
        prov = self._providers.get(playlist_id)
        if prov is not None:
            prov.active = index
        self.storage.set_servers(playlist_id, active=index)
        if old != index:
            self._server_switched(playlist_id, old, index)
            self.pages["lists"].refresh()

    def _server_switched(self, playlist_id: int, old: int, new: int) -> None:
        """Tell the user when the main server went down or came back."""
        row = self.storage.get_playlist(playlist_id)
        name = row["name"] if row is not None else ""
        if old == 0 and new != 0:
            text = _("“{name}”: the main server is not answering. Using the backup "
                     "server.").format(name=name)
        elif new == 0:
            text = _("“{name}”: the main server is back.").format(name=name)
        else:
            return
        self._toast(text)
        app = self.get_application()
        if self.settings.get("server_alerts") and app is not None:
            note = Gio.Notification.new(_("AshyTV servers"))
            note.set_body(text)
            app.send_notification(f"server-{playlist_id}", note)

    def _stream_failover(self, item: PlayItem) -> str | None:
        """Playback failed or kept freezing: rebuild the item's URLs on the
        next server of its playlist. None when there is no other server."""
        prov = self._providers.get(item.playlist_id)
        if prov is None or len(prov.servers) < 2 or item.failovers >= len(prov.servers) - 1:
            return None
        old_index, old = prov.active, prov.server
        nxt = failover.ServerPool(prov.servers, old_index).next_after(old_index)
        if nxt is None:
            return None
        item.failovers += 1
        prov.active = nxt
        new = prov.server
        item.url = failover.swap_server(item.url, old, new)
        for variant in item.variants:
            variant.url = failover.swap_server(variant.url, old, new)
        self._mark_down(item.playlist_id, old)
        self._set_active_server(item.playlist_id, nxt)
        return (_("Switching to the main server…") if nxt == 0
                else _("Switching to the backup server…"))

    def _mark_down(self, playlist_id: int, url: str) -> None:
        servers = self._servers_of(playlist_id)
        health = failover.health_list(self.storage.server_health(playlist_id), servers)
        for h in health:
            if h.url == url:
                h.status, h.latency_ms, h.checked = failover.DOWN, 0, time.time()
        self.storage.set_server_health(playlist_id, [h.to_dict() for h in health])

    def _on_servers_changed(self, _w, playlist_id: int, backups) -> None:
        servers = self._servers_of(playlist_id)
        main = servers[0] if servers else ""
        self.storage.set_servers(playlist_id, failover.clean_backups(backups, main), 0)
        self._load_providers()
        self.pages["lists"].refresh()
        self._check_servers([playlist_id])

    def _periodic_check(self) -> bool:
        self._check_servers([int(r["id"]) for r in self.storage.list_playlists()
                             if r["enabled"]])
        return GLib.SOURCE_CONTINUE

    def _check_once(self, playlist_ids: list[int]) -> bool:
        self._check_servers(playlist_ids)
        return GLib.SOURCE_REMOVE

    def _check_servers(self, playlist_ids: list[int]) -> None:
        """Health check (status, latency) of each server, in the background."""
        jobs = []
        for pid in playlist_ids:
            servers = self._servers_of(pid)
            if not servers or pid in self._health_running:
                continue
            prov = self._providers.get(pid)
            if prov is not None:
                probe = failover.xtream_probe(prov.username, prov.password,
                                              lambda url: http.fetch_bytes(url, 10))
            else:
                probe = failover.m3u_probe(lambda url: http.fetch_head(url))
            self._health_running.add(pid)
            jobs.append((pid, servers, probe))

        def worker(pid, servers, probe):
            results = [failover.check(url, probe) for url in servers]
            GLib.idle_add(self._servers_checked, pid, results)

        for job in jobs:
            threading.Thread(target=worker, args=job, daemon=True).start()

    def _servers_checked(self, playlist_id: int, results: list) -> bool:
        self._health_running.discard(playlist_id)
        if self.storage.get_playlist(playlist_id) is None:
            return GLib.SOURCE_REMOVE
        self.storage.set_server_health(playlist_id, [h.to_dict() for h in results])
        active = self.storage.active_server(playlist_id)
        up = [i for i, h in enumerate(results) if h.status != failover.DOWN]
        if up and up[0] == 0 and active != 0:
            self._set_active_server(playlist_id, 0)  # the main server is back
        elif up and active < len(results) and results[active].status == failover.DOWN:
            self._set_active_server(playlist_id, up[0])
        self.pages["lists"].refresh()
        return GLib.SOURCE_REMOVE

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

    # ---- scheduled recordings -----------------------------------------

    def _on_record(self, _page, channel: Channel, prog) -> None:
        name = channel.base_name if channel.variant_labels else channel.display_name
        s = Scheduled(playlist_id=channel.playlist_id, channel_url=channel.url,
                      channel_name=name, title=prog.title or _("Programme"),
                      start=int(prog.start), stop=int(prog.stop))
        clashes = self.recordings.clashes(s)
        if not self.recordings.toggle(s):
            self._cancel_recorder(s.key)
            self._save_recordings()
            self._toast(_("Recording cancelled"))
            return
        self._save_recordings()
        when = GLib.DateTime.new_from_unix_local(s.start).format("%R")
        if clashes:
            # Not refused: an account with enough connections records both.
            # But a single-connection account cannot, and finding out
            # afterwards means an empty file and a ruined evening.
            self._toast(_("“{title}” will be recorded at {time}, at the same time as "
                          "“{other}”. Your provider has to allow two connections at once.")
                        .format(title=s.title, time=when, other=clashes[0].title))
        else:
            self._toast(_("“{title}” will be recorded at {time}").format(
                title=s.title, time=when))

    def _on_cancel_scheduled(self, _page, key: str) -> None:
        s = self.recordings.get(key)
        running = s is not None and s.state == rec.RECORDING
        self._cancel_recorder(key)
        if not running:
            self.recordings.remove(key)
        self._save_recordings()
        self._toast(_("Recording stopped.") if running else _("Recording cancelled"))

    def _save_recordings(self) -> None:
        self.settings.set("scheduled_recordings", self.recordings.to_list())
        self.pages["recordings"].invalidate()

    def _reset_recordings(self) -> None:
        """Nothing is recording at start-up, whatever the list last said."""
        changed = False
        for s in self.recordings:
            if s.state == rec.RECORDING:
                s.state = rec.FAILED
                s.error = _("The app was closed during the recording.")
                # Nothing was ever written to it: leave no empty file behind.
                try:
                    f = Path(s.path)
                    if s.path and f.exists() and f.stat().st_size == 0:
                        f.unlink()
                        s.path = ""
                except OSError:
                    pass
                changed = True
        for s in self.recordings.missed(time.time()):
            s.state = rec.FAILED
            s.error = _("The app was not running when it started.")
            changed = True
        if changed:
            self._save_recordings()

    def _check_recordings(self) -> bool:
        now = time.time()
        changed = False
        for s in self.recordings.over(now):
            self._cancel_recorder(s.key)
            changed = True
        for s in self.recordings.missed(now):
            s.state = rec.FAILED
            s.error = _("The app was not running when it started.")
            changed = True
        for s in self.recordings.due(now):
            if self._start_recording(s):
                changed = True
        if self.recordings.prune(now):
            changed = True
        if changed:
            self._save_recordings()
        return GLib.SOURCE_CONTINUE

    def _start_recording(self, s: Scheduled) -> bool:
        channel = self.storage.find_channel(s.playlist_id, s.channel_url)
        if channel is None:
            s.state = rec.FAILED
            s.error = _("This channel is no longer in your playlists.")
            return True
        folder = recordings_dir()
        stem = safe_name(f"{s.channel_name} - {s.title}", _("Recording"))
        path = folder / f"{stem} {time.strftime('%Y-%m-%d %H-%M', time.localtime(s.start))}.ts"
        worker = Recording(self._resolve(channel.url, channel.playlist_id), path, until=s.ends,
                           on_done=lambda w, k=s.key: GLib.idle_add(self._recording_done, k),
                           on_fail=lambda w, why, k=s.key: GLib.idle_add(
                               self._recording_failed, k, why))
        self._recorders[s.key] = worker
        s.state = rec.RECORDING
        s.path = str(path)
        worker.start()
        self._toast(_("Recording “{title}”…").format(title=s.title))
        return True

    def _cancel_recorder(self, key: str) -> None:
        worker = self._recorders.pop(key, None)
        if worker is not None:
            worker.stop()
        s = self.recordings.get(key)
        if s is not None and s.state == rec.RECORDING:
            s.state = rec.DONE

    def _recording_done(self, key: str) -> bool:
        self._recorders.pop(key, None)
        s = self.recordings.get(key)
        if s is not None and s.state == rec.RECORDING:
            s.state = rec.DONE
            self._save_recordings()
            self._toast(_("“{title}” was recorded.").format(title=s.title))
        return GLib.SOURCE_REMOVE

    def _recording_failed(self, key: str, why: str) -> bool:
        self._recorders.pop(key, None)
        s = self.recordings.get(key)
        if s is not None:
            s.state = rec.FAILED
            s.error = why
            self._save_recordings()
            self._toast(_("“{title}” could not be recorded.").format(title=s.title))
        return GLib.SOURCE_REMOVE

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
        # A dialog left open would sit over the video and keep the keys.
        dialog = self.get_visible_dialog()
        if dialog is not None:
            dialog.force_close()
        self._play_channel(channel)

    def _on_groups_setting(self, _value) -> None:
        self.storage.group_order = list(self.settings.get("group_order") or [])
        self._invalidate_pages("home", "channels", "favorites")

    # ---- hidden groups and channels -----------------------------------------

    def _hidden_changed(self) -> None:
        """Something was hidden or shown again: every list, rail, guide and
        the zapping order follow (the page on screen reloads now)."""
        self._invalidate_pages("home", "channels", "favorites", "movies", "series")
        self._update_hidden_counts()

    def _update_hidden_counts(self) -> None:
        """'Hidden (N)' entries and the Settings row, right away."""
        for key in ("channels", "movies", "series"):
            self.pages[key].update_hidden_count()
        self.pages["settings"].update_hidden()

    def show_hidden(self) -> None:
        """The dialog to see what is hidden and show it again."""
        dialog = self.get_visible_dialog()
        if isinstance(dialog, HiddenDialog):
            return
        HiddenDialog(self.storage, self._hidden_changed).present(self)

    def _hide_group(self, media_type: MediaType, group: str) -> None:
        entries = self.storage.hide_group(media_type, group)
        if not entries:
            self._toast(_busy_message())
            return
        self._hidden_changed()
        title = (_("Group “{name}” hidden") if media_type is MediaType.LIVE
                 else _("Category “{name}” hidden"))
        self._undo_toast(title.format(name=group), entries)

    def _on_hide_channel(self, page, channel: Channel) -> None:
        entries = self.storage.hide_channel(channel)
        if not entries:
            self._toast(_busy_message())
            return
        page.channel_hidden(channel)  # in place: the list keeps its scroll
        for key in ("home", "channels", "favorites"):
            if self.pages[key] is not page:
                self.pages[key].invalidate()
        self._update_hidden_counts()
        name = channel.base_name if channel.variant_labels else channel.display_name
        self._undo_toast(_("Channel “{name}” hidden").format(name=name), entries)

    def _undo_toast(self, title: str, entries: list) -> None:
        """'… hidden. To show it again: Settings → …' with Undo and See hidden
        (a toast has one button: the second one lives in its custom title)."""
        text = Gtk.Label(label=f"{title}\n"
                         + _("To show it again: Settings → Hidden channels and groups"),
                         wrap=True, max_width_chars=46, xalign=0)
        see = Gtk.Button(label=_("See hidden"), valign=Gtk.Align.CENTER)
        see.add_css_class("flat")
        content = Gtk.Box(spacing=10)
        content.append(text)
        content.append(see)
        toast = Adw.Toast(custom_title=content, timeout=8, button_label=_("Undo"))

        def undo(*_a):
            self.storage.unhide(entries)
            self._hidden_changed()

        def see_hidden(*_a):
            toast.dismiss()
            self.show_hidden()

        toast.connect("button-clicked", undo)
        see.connect("clicked", see_hidden)
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

    def _on_sort_az(self, value) -> None:
        self.storage.sort_az = bool(value)
        self._invalidate_pages()

    def _on_hide_adult(self, value) -> None:
        self.storage.hide_adult = bool(value)
        self._invalidate_pages()

    # ---- keyboard / utils ------------------------------------------------

    def _install_keys(self) -> None:
        # Keyboard mode first (capture): it must see every key and pointer
        # event, including those a dialog or popover handles.
        self.keyboard = focus_util.KeyboardMode()
        mode_keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        mode_keys.connect("key-pressed", self._on_any_key)
        self.add_controller(mode_keys)
        pointer = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        pointer.connect("event", self._on_pointer_event)
        self.add_controller(pointer)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        # Bubble phase: only keys no widget used (e.g. ← at the left edge).
        nav_keys = Gtk.EventControllerKey()
        nav_keys.connect("key-pressed", self._on_nav_key)
        self.add_controller(nav_keys)
        # The focus must stay visible: scroll to it, and never lose it.
        self._focus_check = 0
        self.connect("notify::focus-widget", self._on_focus_changed)
        # GTK turns focus-visible off a few seconds after the last key: keep
        # it on while the keyboard is in use, so the ring never vanishes.
        self.connect("notify::focus-visible", self._on_focus_visible)

    _POINTER_EVENTS = (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.SCROLL,
                       Gdk.EventType.TOUCH_BEGIN)

    def _on_any_key(self, _c, keyval, _code, _state) -> bool:
        if keyval in _MODIFIER_KEYS:
            return False
        if self.keyboard.key():
            self._set_keyboard_mode(True)
            if self.get_focus() is None and self.root_stack.get_visible_child_name() == "shell":
                self.nav.focus_active()  # app start: show where we are
        return False

    def _on_pointer_event(self, _c, event) -> bool:
        # GtkEventControllerLegacy may hand over no event (e.g. crossing/grab).
        if event is None:
            return False
        kind = event.get_event_type()
        if kind == Gdk.EventType.MOTION_NOTIFY:
            ok, x, y = event.get_position()
            if ok and self.keyboard.motion(x, y):
                self._set_keyboard_mode(False)
        elif kind in self._POINTER_EVENTS and self.keyboard.pointer():
            self._set_keyboard_mode(False)
        return False

    def _set_keyboard_mode(self, on: bool) -> None:
        # Hover highlights are hidden with the keyboard (CSS: .ashy-keyboard):
        # a resting pointer must not look like a second selection.
        if on:
            self.add_css_class("ashy-keyboard")
        else:
            self.remove_css_class("ashy-keyboard")
        self.set_focus_visible(on)
        if on:
            self._on_focus_changed()

    def _on_focus_visible(self, *_a) -> None:
        if self.keyboard.active and not self.get_focus_visible():
            GLib.idle_add(self._reassert_focus_visible)

    def _reassert_focus_visible(self) -> bool:
        if self.keyboard.active:
            self.set_focus_visible(True)
        return GLib.SOURCE_REMOVE

    def _on_focus_changed(self, *_a) -> None:
        if not self.keyboard.active or self._focus_check:
            return  # pointer use: leave scrolling to the user
        self._focus_check = GLib.idle_add(self._check_focus)

    def _check_focus(self) -> bool:
        self._focus_check = 0
        focus = self.get_focus()
        if isinstance(focus, (Gtk.ScrolledWindow, Gtk.Viewport)):
            # A scrolled area with nothing focusable inside (an empty page)
            # takes the focus with no visible ring: skip it from now on.
            focus.set_focusable(False)
            if focus.child_focus(Gtk.DirectionType.TAB_FORWARD):
                return GLib.SOURCE_REMOVE
            dialog = self.get_visible_dialog()
            if dialog is not None and dialog.child_focus(Gtk.DirectionType.TAB_FORWARD):
                return GLib.SOURCE_REMOVE
            focus = None
        if focus_util.is_visible(focus):
            focus_util.reveal(focus)
            return GLib.SOURCE_REMOVE
        # Lost (a list was rebuilt, a page hidden…): back to the page.
        if (self.root_stack.get_visible_child_name() == "shell"
                and self.get_visible_dialog() is None
                and not (self.tour is not None and self.tour.active)
                and not self.focus_page()):
            self.nav.focus_active()
        return GLib.SOURCE_REMOVE

    def _on_key(self, _c, keyval, _code, state) -> bool:
        # Keyboard mode (visible focus ring) first: GTK runs this controller
        # before the one installed for it, and an arrow handled here stopped
        # there — the focus moved but its ring never showed.
        self._on_any_key(None, keyval, _code, state)
        if self.tour is not None and self.tour.active:
            return self.tour.handle_key(keyval)
        # A dialog is a child of the window, so this capture-phase handler
        # sees its keys too: leave them to the dialog (typing must not open
        # the search behind it, Esc must close it).
        if self.get_visible_dialog() is not None:
            return False
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
        if direction == Gtk.DirectionType.RIGHT and self.nav.has_focus_within(focus):
            # Leaving the sidebar enters the content — of the page whose item
            # has the focus, opening it first if needed.
            key = self.nav.key_of(focus)
            if key is not None and key != self._current:
                self.navigate(key)
                GLib.timeout_add(_PAGE_SWITCH_MS, self._enter_page)
                return True
            # Back to the item it left the page from (as on the TVs), else the page.
            left = getattr(self, "_left_from", None)
            page = self.pages.get(self._current)
            if (left is not None and page is not None and left.is_ancestor(page)
                    and focus_util.is_visible(left) and left.grab_focus()):
                focus_util.reveal(left)
                return True
            if self.focus_page():
                return True
        home = self.pages.get("home")
        if (direction in (Gtk.DirectionType.LEFT, Gtk.DirectionType.RIGHT)
                and home is not None and home.hero_has_focus(focus)
                and home.hero_step(-1 if direction == Gtk.DirectionType.LEFT else 1)):
            return True  # the featured banner: previous / next title
        if not focus.get_mapped():
            # The focused widget was hidden (emptied list, closed view…).
            return self.focus_page() or self.nav.focus_active() or True
        moved = keynav.move(self, direction, prefer={self.nav: self.nav.focus_active},
                            walls=(self.nav,))
        in_page = focus.is_ancestor(self.page_stack)
        if (direction == Gtk.DirectionType.LEFT and in_page and self.get_focus() is focus
                and not keynav.keeps_key(focus, direction)):
            # Nothing more to the left on the page (the start of a row lower
            # than the menu's items): ← opens the menu, on the current page.
            self.nav.focus_active()
        if in_page and self.nav.has_focus_within(self.get_focus()):
            self._left_from = focus  # → comes back here
        return moved

    def _enter_page(self) -> bool:
        """Put the focus on the page just opened (after its transition)."""
        if not self.focus_page():
            self.nav.focus_active()
        return GLib.SOURCE_REMOVE

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
        if self.get_visible_dialog() is not None:
            return False
        if (self.root_stack.get_visible_child_name() != "shell"
                or state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)):
            return False
        focus = self.get_focus()
        in_nav = self.nav.has_focus_within(focus)
        if not in_nav and keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Escape):
            if keyval == Gdk.KEY_BackSpace and self._focus_is_editable():
                return False
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
        focus = self.get_focus()
        popover = focus.get_ancestor(Gtk.Popover) if focus is not None else None
        if popover is not None and keyval in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            popover.popdown()  # like Esc on a keyboard: back to what opened it
            return True
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
        done = ((hasattr(page, "focus_content") and page.focus_content())
                or page.child_focus(Gtk.DirectionType.TAB_FORWARD))
        focus = self.get_focus()
        if done and focus is not None:
            focus_util.reveal(focus)  # the same widget again: no focus signal
        return done

    def _on_close_request(self, _w) -> bool:
        self.pages["channels"].shutdown()
        if self.mini is not None:
            self._close_mini(stop=True)
        if self.player is not None:
            self.player.shutdown()
        return False

    def _toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))


def _busy_message() -> str:
    return _("Could not save now: a playlist is being updated. Try again.")


def activate_widget(widget: Gtk.Widget) -> bool:
    """Activate the focused widget like Enter would.

    Buttons, list rows and list/grid items all bind Enter to their
    "activate" signal, so widget.activate() covers every case.
    """
    return widget.activate()
