"""Movie/series detail dialog (synopsis, details, seasons and episodes)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date

from gi.repository import Adw, GLib, GObject, Gtk

from .. import compat
from ..i18n import _, ngettext
from ..services import http
from ..models import Channel, Episode, MediaType
from .cast import CastRow, poster_section
from .pages._common import set_picture


def fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class DetailDialog(Adw.Dialog):
    __gtype_name__ = "AshyDetailDialog"

    __gsignals__ = {
        "play": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "favorite-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # a CastMember was clicked
        "show-person": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # a title of "More like this" was clicked (a Channel)
        "open-title": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, storage, channel: Channel,
                 load_episodes: Callable[[Channel, Callable, Callable], None],
                 load_info: Callable[[Channel, Callable, Callable], None] | None = None,
                 load_cast: Callable[[Channel, dict, Callable], None] | None = None):
        is_series = channel.media_type is MediaType.SERIES
        super().__init__(title=channel.display_name, content_width=920,
                         content_height=640 if is_series else -1)
        self.storage = storage
        self.channel = channel
        self._episodes: list[Episode] = []
        self._seasons: list[int] = []
        self._load_cast = load_cast
        self._cast_cards = load_cast is not None
        # The provider's details (movies) and the panel's TMDB data, merged
        # whenever one of them arrives.
        self._info: dict = {}
        self._details = None

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("flat")
        view.add_top_bar(header)
        view.set_extend_content_to_top_edge(True)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        # ---- top: backdrop + poster + title ----
        hero = Gtk.Overlay()
        # Fixed height: the image is an overlay (does not affect the size).
        base = Gtk.Box()
        base.set_size_request(-1, 300)
        base.add_css_class("ashy-hero-fallback")
        hero.set_child(base)
        hero.set_overflow(Gtk.Overflow.HIDDEN)
        backdrop = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        set_picture(backdrop, channel.backdrop or channel.logo, 780)
        hero.add_overlay(backdrop)
        self.backdrop = backdrop
        shade = Gtk.Box()
        shade.add_css_class("ashy-hero-shade")
        hero.add_overlay(shade)

        info = Gtk.Box(spacing=22, valign=Gtk.Align.END)
        info.set_margin_start(28)
        info.set_margin_end(28)
        info.set_margin_bottom(20)
        poster_frame = Gtk.Box()
        poster_frame.set_overflow(Gtk.Overflow.HIDDEN)
        poster_frame.add_css_class("ashy-poster-img")
        poster = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        poster.set_size_request(140, 210)
        set_picture(poster, channel.logo, 210)
        poster_frame.append(poster)
        info.append(poster_frame)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        valign=Gtk.Align.END, hexpand=True)
        title = Gtk.Label(label=channel.display_name, xalign=0, wrap=True)
        title.add_css_class("ashy-hero-title")
        meta_lbl = Gtk.Label(xalign=0, wrap=True, use_markup=True)
        meta_lbl.add_css_class("ashy-hero-sub")
        self.meta_lbl = meta_lbl
        # Original title (filled in by the movie details, when different).
        self.orig_lbl = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.orig_lbl.add_css_class("ashy-hero-orig")
        texts.append(title)
        texts.append(self.orig_lbl)
        texts.append(meta_lbl)

        actions = Gtk.Box(spacing=10)
        self.play_btn = Gtk.Button(label=_("Watch"))
        self.play_btn.add_css_class("pill-light")
        self.play_btn.connect("clicked", self._on_play_main)
        self.fav_btn = Gtk.Button()
        self.fav_btn.add_css_class("circular")
        self.fav_btn.add_css_class("ashy-heart")
        self.fav_btn.connect("clicked", self._toggle_fav)
        self._paint_fav()
        actions.append(self.play_btn)
        actions.append(self.fav_btn)
        texts.append(actions)
        info.append(texts)
        hero.add_overlay(info)
        page.append(hero)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(28)
        body.set_margin_end(28)
        body.set_margin_bottom(24)
        self.plot_lbl = Gtk.Label(label=channel.plot, xalign=0, wrap=True,
                                  visible=bool(channel.plot))
        self.plot_lbl.add_css_class("ashy-page-subtitle")
        body.append(self.plot_lbl)
        self.tagline_lbl = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.tagline_lbl.add_css_class("ashy-tagline")
        body.prepend(self.tagline_lbl)
        # Details like in a media center: crew, studios, technical info, links.
        self.facts = Gtk.Grid(column_spacing=24, row_spacing=8, visible=False)
        self.facts.add_css_class("ashy-facts")
        body.append(self.facts)

        if channel.media_type is MediaType.SERIES:
            row = Gtk.Box(spacing=12)
            lbl = Gtk.Label(label=_("Episodes"), xalign=0, hexpand=True)
            lbl.add_css_class("ashy-section-title")
            self.season_dd = Gtk.DropDown(model=Gtk.StringList())
            self.season_dd.connect("notify::selected", lambda *_: self._show_season())
            row.append(lbl)
            row.append(self.season_dd)
            body.append(row)
            self.ep_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.ep_list.add_css_class("boxed-list")
            self.ep_list.connect("row-activated", self._on_episode_row)
            self.ep_stack = Gtk.Stack()
            spinner = compat.spinner(height_request=48, width_request=48)
            self.ep_stack.add_named(spinner, "loading")
            self.ep_stack.add_named(self.ep_list, "list")
            self.ep_error = Adw.StatusPage(icon_name="network-error-symbolic",
                                           title=_("Could not load"))
            self.ep_error.add_css_class("compact")
            self.ep_stack.add_named(self.ep_error, "error")
            body.append(self.ep_stack)
            self.play_btn.set_sensitive(False)
            load_episodes(channel, self._episodes_loaded, self._episodes_failed)
        else:
            self._update_resume_label()
            self.links = Gtk.Box(spacing=6, visible=False)
            body.append(self.links)
            self.info_spinner = compat.spinner(height_request=24, width_request=24,
                                            halign=Gtk.Align.START,
                                            visible=load_info is not None)
            body.append(self.info_spinner)

        # Cast: actor cards (photos from TMDB, or names from the provider).
        self.cast_row = CastRow()
        self.cast_row.connect("person", lambda _r, member: self.emit("show-person", member))
        body.append(self.cast_row)
        # "More like this": TMDB's recommendations that are in the user's lists.
        self.more_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
        body.append(self.more_box)
        self._render_meta()

        page.append(body)
        # Fixed height with a scrolling body: the details arrive later and a
        # long synopsis or cast must not push the dialog off screen.
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(page)
        view.set_content(sw)
        self.set_child(view)
        self._page = page
        if not is_series:
            self._fit_height()
            # Only now: cached details answer right away, and the dialog must
            # already be built when they arrive.
            if load_info is not None:
                load_info(channel, self._info_loaded, self._info_failed)
            else:
                self._request_cast({})

    # ---- movie ---------------------------------------------------------

    def _update_resume_label(self) -> None:
        pos = self.storage.resume_position(self.channel.playlist_id, self.channel.url)
        self.play_btn.set_label(_("Resume from {time}").format(time=fmt_time(pos))
                                if pos > 0 else _("Watch"))

    def _fit_height(self) -> None:
        """Movies: as tall as their content (up to 640 px), so there is no
        scrollbar and no empty strip below."""
        _min, nat, _b1, _b2 = self._page.measure(Gtk.Orientation.VERTICAL, 920)
        self.set_content_height(min(640, nat))

    def _request_cast(self, info: dict) -> None:
        load, self._load_cast = self._load_cast, None  # once per dialog
        if load is not None:
            load(self.channel, info, self._cast_loaded)

    def _cast_loaded(self, members: list, _from_tmdb: bool, details=None, similar=()) -> None:
        self.cast_row.set_cast(members)
        if details is not None:
            self._details = details
            if details.overview and not self.plot_lbl.get_label():
                self.plot_lbl.set_label(details.overview)
                self.plot_lbl.set_visible(True)
            if details.tagline:
                self.tagline_lbl.set_label(details.tagline)
                self.tagline_lbl.set_visible(True)
            orig = details.original_title
            if orig and orig.casefold() not in self.channel.display_name.casefold():
                self.orig_lbl.set_label(orig)
                self.orig_lbl.set_visible(True)
            if details.backdrop and not self.channel.backdrop and not self._info.get("backdrop"):
                set_picture(self.backdrop, details.backdrop, 780)
            self._render_meta()
            self._render_facts()
        if similar:
            self.more_box.append(poster_section(_("More like this"), list(similar),
                                                lambda ch: self.emit("open-title", ch)))
            self.more_box.set_visible(True)
        if (members or details is not None) and self.channel.media_type is not MediaType.SERIES:
            self._fit_height()

    def _render_meta(self) -> None:
        """'2011 • 1 h 49 min • ★ 5.5 • Ends at 19:21' with a yellow star."""
        info, d = self._info, self._details
        ch = self.channel
        duration = info.get("duration", 0) or (d.runtime * 60 if d is not None else 0)
        parts: list[str] = []

        def add(text: str) -> None:
            # Skip empty parts and repeats (genre and category are often equal).
            if text and text.casefold() not in (p.casefold() for p in parts):
                parts.append(text)

        add(ch.year)
        if duration:
            h, m = divmod(duration // 60, 60)
            if ch.media_type is MediaType.SERIES:
                add(_("{m} min per episode").format(m=duration // 60))
            else:
                add(_("{h} h {m} min").format(h=h, m=m) if h else _("{m} min").format(m=m))
        add(ch.genre if d is None or not d.genres else "")
        add(info.get("age", ""))
        add(ch.group)
        markup = [GLib.markup_escape_text(p) for p in parts]
        rating = d.rating if d is not None and d.rating else ch.rating
        if rating:
            star = f'<span foreground="#f5c518">★</span> <b>{rating:.1f}</b>'
            markup.insert(min(len(markup), 2 if duration else 1), star)
        if duration and ch.media_type is not MediaType.SERIES:
            remaining = duration - self.storage.resume_position(ch.playlist_id, ch.url)
            ends = time.strftime("%H:%M", time.localtime(time.time() + remaining))
            markup.append(GLib.markup_escape_text(_("Ends at {time}").format(time=ends)))
        self.meta_lbl.set_markup("  •  ".join(markup))

    def _render_facts(self) -> None:
        info, d = self._info, self._details

        def names(items) -> str:
            return ", ".join(items) if items else ""

        is_series = self.channel.media_type is MediaType.SERIES
        rows = [(_("Created by") if is_series else _("Director"),
                 names(d.directors) if d is not None and d.directors else info.get("director")),
                (_("Writers"), names(d.writers) if d is not None else ""),
                (_("Studios"), names(d.studios) if d is not None else ""),
                (_("Genres"), names(d.genres) if d is not None and d.genres
                 else info.get("genre") or (self.channel.genre if is_series else "")),
                (_("Tags"), names(d.keywords) if d is not None else ""),
                (_("Video"), info.get("video")), (_("Audio"), info.get("audio")),
                # With a cast loader the names are cards below, not a line here.
                (_("Cast"), "" if self._cast_cards else info.get("cast")),
                (_("Country"), info.get("country")),
                (_("Released"), _format_date(info.get("released", ""))),
                (_("Subtitles"), ngettext("{n} file", "{n} files",
                                          len(info["subtitles"])).format(
                                              n=len(info["subtitles"]))
                 if info.get("subtitles") else "")]
        while (child := self.facts.get_first_child()) is not None:
            self.facts.remove(child)
        row = 0
        for name, value in rows:
            if not value:
                continue
            key = Gtk.Label(label=name, xalign=0, valign=Gtk.Align.START)
            key.add_css_class("dim-label")
            # Not selectable: a selectable label is a Tab stop with no visible
            # focus, and one made unfocusable traps Tab (GTK keeps it).
            val = Gtk.Label(label=value, xalign=0, wrap=True, hexpand=True)
            val.add_css_class("ashy-fact-value")
            self.facts.attach(key, 0, row, 1, 1)
            self.facts.attach(val, 1, row, 1, 1)
            row += 1
        self.facts.set_visible(row > 0)

    def _info_loaded(self, info: dict) -> None:
        self.info_spinner.set_visible(False)
        self._request_cast(info or {})
        if not info:
            self._fit_height()
            return
        if info.get("plot") and len(info["plot"]) > len(self.plot_lbl.get_label()):
            self.plot_lbl.set_label(info["plot"])
            self.plot_lbl.set_visible(True)
        orig = info.get("original_title", "")
        if orig and orig.casefold() not in self.channel.display_name.casefold():
            self.orig_lbl.set_label(orig)
            self.orig_lbl.set_visible(True)
        if info.get("backdrop") and not self.channel.backdrop:
            set_picture(self.backdrop, info["backdrop"], 780)
        self._info = info
        self._render_meta()
        self._render_facts()
        for label, url in ((_("Trailer"), info.get("trailer")), ("TMDB", info.get("tmdb_url"))):
            if url:
                self.links.append(Gtk.LinkButton(uri=url, label=label))
        self.links.set_visible(self.links.get_first_child() is not None)
        self._fit_height()

    def _info_failed(self, _exc: Exception) -> None:
        self.info_spinner.set_visible(False)  # the basic data is still shown
        self._request_cast({})
        self._fit_height()

    def _on_play_main(self, _b) -> None:
        if self.channel.media_type is MediaType.SERIES:
            ep = self._next_episode()
            if ep is not None:
                self.emit("play", self.channel, ep)
        else:
            self.emit("play", self.channel, None)
        self.close()

    # ---- series --------------------------------------------------------

    def _episodes_loaded(self, episodes: list[Episode]) -> None:
        self._request_cast({})  # after the episodes: their request brings the cast text
        self._episodes = episodes
        if not episodes:
            self.ep_error.set_title(_("No episodes available"))
            self.ep_stack.set_visible_child_name("error")
            return
        seasons = sorted({e.season for e in episodes})
        self._seasons = seasons
        self.season_dd.set_model(Gtk.StringList.new([_("Season {n}").format(n=s) for s in seasons]))
        nxt = self._next_episode()
        if nxt is not None and nxt.season in seasons:
            self.season_dd.set_selected(seasons.index(nxt.season))
        self._show_season()
        self.ep_stack.set_visible_child_name("list")
        self.play_btn.set_sensitive(True)
        if nxt is not None:
            self.play_btn.set_label(_("Watch {label}").format(label=nxt.label))

    def _episodes_failed(self, exc: Exception) -> None:
        self._request_cast({})
        self.ep_error.set_description(GLib.markup_escape_text(http.describe_error(exc)))
        self.ep_stack.set_visible_child_name("error")

    def _next_episode(self) -> Episode | None:
        """Last watched episode (if unfinished) or the next one."""
        if not self._episodes:
            return None
        watched = {r["url"]: r for r in self.storage.history(500, MediaType.SERIES)
                   if r["playlist_id"] == self.channel.playlist_id}
        last_idx = -1
        last_time = 0
        for i, ep in enumerate(self._episodes):
            r = watched.get(ep.url)
            if r and r["watched_at"] > last_time:
                last_idx, last_time = i, r["watched_at"]
        if last_idx < 0:
            return self._episodes[0]
        r = watched[self._episodes[last_idx].url]
        done = r["duration"] and r["position"] > r["duration"] * 0.95
        if done and last_idx + 1 < len(self._episodes):
            return self._episodes[last_idx + 1]
        return self._episodes[last_idx]

    def _show_season(self) -> None:
        if not self._episodes:
            return
        idx = self.season_dd.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._seasons):
            return
        season = self._seasons[idx]
        self.ep_list.remove_all()
        history = {r["url"]: r for r in self.storage.history(500, MediaType.SERIES)}
        for ep in self._episodes:
            if ep.season == season:
                self.ep_list.append(self._episode_row(ep, history.get(ep.url)))

    def _episode_row(self, ep: Episode, hist) -> Gtk.Widget:
        row = Adw.ActionRow(title=_escape(ep.title or _("Episode {n}").format(n=ep.episode)),
                            subtitle=_escape(ep.plot[:160]) if ep.plot else "",
                            activatable=True, subtitle_lines=2)
        row._episode = ep  # type: ignore[attr-defined]
        num = Gtk.Label(label=f"E{ep.episode}", width_chars=4)
        num.add_css_class("ashy-chan-num")
        row.add_prefix(num)
        suffix = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                         valign=Gtk.Align.CENTER)
        if ep.duration:
            d = Gtk.Label(label=fmt_time(ep.duration))
            d.add_css_class("ashy-faint")
            suffix.append(d)
        if hist and hist["duration"]:
            bar = Gtk.ProgressBar(fraction=min(1.0, hist["position"] / hist["duration"]))
            bar.add_css_class("ashy-progress-mini")
            bar.set_size_request(60, -1)
            suffix.append(bar)
        row.add_suffix(suffix)
        row.add_suffix(Gtk.Image(icon_name="media-playback-start-symbolic"))
        return row

    def _on_episode_row(self, _list, row) -> None:
        ep = getattr(row, "_episode", None)
        if ep is not None:
            self.emit("play", self.channel, ep)
            self.close()

    # ---- favorite ------------------------------------------------------

    def _toggle_fav(self, _b) -> None:
        if not self.storage.set_favorite(self.channel, not self.channel.favorite):
            return
        self._paint_fav()
        self.emit("favorite-changed", self.channel)

    def _paint_fav(self) -> None:
        active = self.channel.favorite
        self.fav_btn.set_icon_name("ashy-heart-filled-symbolic" if active
                                   else "ashy-heart-symbolic")
        self.fav_btn.set_tooltip_text(_("Remove from favorites") if active
                                      else _("Add to favorites"))
        if active:
            self.fav_btn.add_css_class("active")
        else:
            self.fav_btn.remove_css_class("active")


def _format_date(text: str) -> str:
    """'1985-08-09' -> the date in the user's format; other text unchanged."""
    try:
        d = date.fromisoformat(text[:10])
    except ValueError:
        return text
    return GLib.DateTime.new_local(d.year, d.month, d.day, 0, 0, 0).format("%x")


def _escape(text: str) -> str:
    return GLib.markup_escape_text(text, -1)
