"""Movie/series detail dialog (synopsis, seasons and episodes)."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, GObject, Gtk

from ..i18n import _
from ..services import http
from ..models import Channel, Episode, MediaType
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
    }

    def __init__(self, storage, channel: Channel,
                 load_episodes: Callable[[Channel, Callable, Callable], None]):
        is_series = channel.media_type is MediaType.SERIES
        super().__init__(title=channel.display_name, content_width=920,
                         content_height=640 if is_series else -1)
        self.storage = storage
        self.channel = channel
        self._episodes: list[Episode] = []
        self._seasons: list[int] = []

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
        meta = []
        for part in (channel.year, channel.genre,
                     f"★ {channel.rating:.1f}" if channel.rating else "", channel.group):
            # Skip empty parts and repeats (genre and category are often equal).
            if part and part.casefold() not in (m.casefold() for m in meta):
                meta.append(part)
        meta_lbl = Gtk.Label(label="  •  ".join(meta), xalign=0, wrap=True)
        meta_lbl.add_css_class("ashy-hero-sub")
        texts.append(title)
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
        if channel.plot:
            plot = Gtk.Label(label=channel.plot, xalign=0, wrap=True, selectable=True)
            if not is_series:
                plot.set_lines(6)  # movies have no scrolling: keep it short
                plot.set_ellipsize(3)  # Pango.EllipsizeMode.END
                plot.set_tooltip_text(channel.plot)
            plot.add_css_class("ashy-page-subtitle")
            body.append(plot)

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
            spinner = Adw.Spinner(height_request=48, width_request=48)
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

        if body.get_first_child() is not None:
            page.append(body)  # no empty strip under the hero without a synopsis
        if is_series:
            # Series: fixed height, the episode list scrolls.
            sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_child(page)
            view.set_content(sw)
        else:
            # Movies: short content, the dialog takes its height (no scrollbar).
            view.set_content(page)
        self.set_child(view)

    # ---- movie ---------------------------------------------------------

    def _update_resume_label(self) -> None:
        pos = self.storage.resume_position(self.channel.playlist_id, self.channel.url)
        self.play_btn.set_label(_("Resume from {time}").format(time=fmt_time(pos))
                                if pos > 0 else _("Watch"))

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
        self.storage.set_favorite(self.channel, not self.channel.favorite)
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


def _escape(text: str) -> str:
    return GLib.markup_escape_text(text, -1)
