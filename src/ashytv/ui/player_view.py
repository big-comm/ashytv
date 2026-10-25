"""Playback screen: fullscreen video with auto-hiding controls."""

from __future__ import annotations

import os
import re
import time

from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gtk

from ..i18n import _
from ..models import PlayItem
from ..services.quality import QualityController
from ..player.mpv_widget import MpvWidget
from .channel_overlay import ChannelOverlay
from .detail_dialog import fmt_time
from .pages._common import set_logo
from .pages.settings_page import recordings_dir

_HIDE_DELAY = 3500
_DRAG_THRESHOLD = 8       # pixels the pointer must move before a drag starts
_SAVE_EVERY = 10          # seconds between history position saves
_ASPECTS = [(_("Auto"), "no"), ("16:9", "16:9"), ("4:3", "4:3"),
            ("21:9", "2.33:1")]
_SPEEDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
_SEEK_DELAY_MS = 150      # wait for the pointer to rest before seeking
_QUALITY_TICK = 5         # seconds between adaptive quality checks
_BUFFER_FULL_SECS = 8     # buffered seconds that count as a healthy stream
_RANKS = {"4K": 4, "FHD": 3, "HD": 2, "SD": 1}
_DIGIT_DELAY_MS = 1500    # wait after the last digit before tuning
_LIVE_EDGE_SECS = 8       # closer than this to the live edge counts as live
_DIGITS = {**{getattr(Gdk, f"KEY_{n}"): n for n in range(10)},
           **{getattr(Gdk, f"KEY_KP_{n}"): n for n in range(10)}}


class PlayerView(Gtk.Overlay):
    __gtype_name__ = "AshyPlayerView"

    __gsignals__ = {
        "closed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "zap": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "toggle-fullscreen": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "favorite-toggled": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "pip-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # item, position, duration — for "continue watching"
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (object, float, float)),
        "ended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "channel-list": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "zap-to": (GObject.SignalFlags.RUN_FIRST, None, (int,)),  # index (0-based)
        "zap-previous": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.add_css_class("ashy-player")
        self._guide = None
        self._item: PlayItem | None = None
        self._hide_source = 0
        self._click_source = 0
        self._osd_source = 0
        self._last_motion = (-1.0, -1.0)
        self._last_save = 0.0
        self._can_zap = False
        self._fullscreen = False
        self._recording: str | None = None
        self._rec_started = 0.0
        self._seeking = False
        self._seek_source = 0
        self._quality: QualityController | None = None

        self.mpv = MpvWidget(hwdec=bool(settings.get("hwdec")),
                             cache_secs=int(settings.get("cache_secs")))
        self.mpv.volume = float(settings.get("volume"))
        self.mpv.connect("state-changed", self._on_state)
        self.mpv.connect("time-changed", self._on_time)
        self.video_holder = Adw.Bin(child=self.mpv)
        self.set_child(self.video_holder)

        self.add_overlay(self._build_top_bar())
        self.add_overlay(self._build_bottom_bar())
        self.add_overlay(self._build_center())
        self.channel_overlay = ChannelOverlay()
        self.channel_overlay.connect("play", self._on_overlay_play)
        self.add_overlay(self.channel_overlay)
        self.add_overlay(self._build_osd())
        self._digits = ""
        self._digit_source = 0

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.video_holder.add_controller(click)

        # Dragging the video moves the window (clicks keep pause/fullscreen).
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-update", self._on_video_drag)
        self.video_holder.add_controller(drag)

        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.video_holder.add_controller(scroll)

        GLib.timeout_add_seconds(30, self._refresh_epg)
        GLib.timeout_add_seconds(_QUALITY_TICK, self._quality_tick)

    # ---- construction ----------------------------------------------------

    def _btn(self, icon: str, tooltip: str, callback=None, main=False) -> Gtk.Button:
        btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip,
                         valign=Gtk.Align.CENTER)
        btn.add_css_class("circular")
        if main:
            btn.add_css_class("ashy-play-main")
        if callback:
            btn.connect("clicked", lambda *_: callback())
        return btn

    def _menu_btn(self, icon: str, tooltip: str, build) -> Gtk.MenuButton:
        btn = Gtk.MenuButton(icon_name=icon, tooltip_text=tooltip,
                             valign=Gtk.Align.CENTER, direction=Gtk.ArrowType.UP)
        btn.add_css_class("circular")
        btn.set_create_popup_func(lambda b: b.set_popover(build()))
        return btn

    def _build_top_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=14, valign=Gtk.Align.START)
        bar.add_css_class("ashy-player-bar")

        bar.append(self._btn("go-previous-symbolic", _("Back (Esc)"), self.close))
        self.num_label = Gtk.Label()
        self.num_label.add_css_class("ashy-player-num")
        bar.append(self.num_label)
        self.logo = Gtk.Image(pixel_size=34, valign=Gtk.Align.CENTER)
        self.logo.add_css_class("ashy-player-logo")
        bar.append(self.logo)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                         valign=Gtk.Align.CENTER, spacing=1)
        self.title_label = Gtk.Label(xalign=0, ellipsize=3)
        self.title_label.add_css_class("ashy-player-title")
        self.prog_label = Gtk.Label(xalign=0, ellipsize=3)
        self.prog_label.add_css_class("ashy-player-prog")
        self.time_label = Gtk.Label(xalign=0)
        self.time_label.add_css_class("ashy-player-time")
        titles.append(self.title_label)
        titles.append(self.prog_label)
        titles.append(self.time_label)
        bar.append(titles)

        self.rec_label = Gtk.Label(visible=False, valign=Gtk.Align.CENTER)
        self.rec_label.add_css_class("ashy-rec-dot")
        bar.append(self.rec_label)
        self.fav_btn = self._btn("ashy-heart-symbolic", _("Favorite"), self._toggle_fav)
        self.fav_btn.add_css_class("ashy-heart")
        self.rec_btn = self._btn("ashy-record-symbolic", _("Record"), self._toggle_record)
        self.pip_btn = self._btn("ashy-pip-symbolic", _("Mini player"),
                                 lambda: self.emit("pip-requested"))
        for b in (self.fav_btn, self.rec_btn, self.pip_btn):
            bar.append(b)
        # WindowHandle: dragging the bar moves the window (buttons still work).
        self.top_bar = Gtk.Revealer(
            child=Gtk.WindowHandle(child=bar), valign=Gtk.Align.START, reveal_child=True,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        return self.top_bar

    def _build_bottom_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      valign=Gtk.Align.END)
        bar.add_css_class("ashy-player-controls")

        self.progress_row = Gtk.Box(spacing=12)
        self.pos_label = Gtk.Label(width_chars=7)
        self.pos_label.add_css_class("ashy-player-time")
        self.scale = Gtk.Scale(hexpand=True, draw_value=False)
        self.scale.set_range(0, 1)
        self.scale.connect("change-value", self._on_seek)
        self.end_label = Gtk.Label(width_chars=7)
        self.end_label.add_css_class("ashy-player-time")
        self.progress_row.append(self.pos_label)
        self.progress_row.append(self.scale)
        self.progress_row.append(self.end_label)
        bar.append(self.progress_row)

        controls = Gtk.CenterBox()
        self.live_badge = Gtk.Label(label=_("● LIVE"), valign=Gtk.Align.CENTER)
        # Timeshift: shows how far behind live you are and jumps back to it.
        self.go_live_btn = Gtk.Button(label=_("Go live"), valign=Gtk.Align.CENTER,
                                      visible=False, tooltip_text=_("Back to live"))
        self.go_live_btn.add_css_class("ashy-go-live")
        self.go_live_btn.connect("clicked", lambda *_: self._go_live())
        live_box = Gtk.Box(spacing=10)
        live_box.append(self.live_badge)
        live_box.append(self.go_live_btn)
        self.live_badge.add_css_class("ashy-rec-dot")
        controls.set_start_widget(live_box)

        center = Gtk.Box(spacing=18)
        self.prev_btn = self._btn("media-skip-backward-symbolic", _("Previous (↑)"),
                                  lambda: self.emit("zap", -1))
        self.back10 = self._btn("media-seek-backward-symbolic", _("Back 10 s (←)"),
                                lambda: self.mpv.seek(-10))
        self.play_btn = self._btn("media-playback-pause-symbolic", _("Pause (Space)"),
                                  self._toggle_pause, main=True)
        self.fwd10 = self._btn("media-seek-forward-symbolic", _("Forward 10 s (→)"),
                               lambda: self.mpv.seek(10))
        self.next_btn = self._btn("media-skip-forward-symbolic", _("Next (↓)"),
                                  lambda: self.emit("zap", 1))
        for b in (self.prev_btn, self.back10, self.play_btn, self.fwd10, self.next_btn):
            center.append(b)
        controls.set_center_widget(center)

        end = Gtk.Box(spacing=6)
        self.vol_btn = self._menu_btn("audio-volume-high-symbolic", _("Volume"),
                                      self._volume_popover)
        self.cc_btn = self._menu_btn("media-view-subtitles-symbolic", _("Subtitles"),
                                     self._subs_popover)
        self.opts_btn = self._menu_btn("emblem-system-symbolic", _("Audio & Video"),
                                       self._options_popover)
        self.fs_btn = self._btn("view-fullscreen-symbolic", _("Fullscreen (F)"),
                                lambda: self.emit("toggle-fullscreen"))
        for b in (self.vol_btn, self.cc_btn, self.opts_btn, self.fs_btn):
            end.append(b)
        controls.set_end_widget(end)
        bar.append(controls)
        self.bottom_bar = Gtk.Revealer(
            child=Gtk.WindowHandle(child=bar), valign=Gtk.Align.END, reveal_child=True,
            transition_type=Gtk.RevealerTransitionType.CROSSFADE)
        self._update_volume_icon()
        return self.bottom_bar

    def _build_center(self) -> Gtk.Widget:
        self.center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                              visible=False)
        self.center.add_css_class("ashy-player-center")
        self.spinner = Adw.Spinner(width_request=42, height_request=42)
        self.center_icon = Gtk.Image(icon_name="network-error-symbolic", pixel_size=40)
        self.center_title = Gtk.Label()
        self.center_title.add_css_class("ashy-poster-title")
        self.center_desc = Gtk.Label(wrap=True, max_width_chars=40,
                                     justify=Gtk.Justification.CENTER)
        self.center_desc.add_css_class("ashy-player-time")
        actions = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.retry_btn = Gtk.Button(label=_("Try again"))
        self.retry_btn.add_css_class("pill-light")
        self.retry_btn.connect("clicked", lambda *_: self._retry())
        actions.append(self.retry_btn)
        self.center_actions = actions
        for w in (self.spinner, self.center_icon, self.center_title, self.center_desc,
                  actions):
            self.center.append(w)
        return self.center

    def _build_osd(self) -> Gtk.Widget:
        self.osd = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.START,
                             margin_top=110, margin_end=28, visible=False)
        self.osd.add_css_class("ashy-player-osd")
        return self.osd

    # ---- API -----------------------------------------------------------

    def set_guide(self, guide) -> None:
        self._guide = guide
        if self._item:
            self._update_info()

    def set_fullscreen(self, fullscreen: bool) -> None:
        self._fullscreen = fullscreen
        icon = "view-restore-symbolic" if fullscreen else "view-fullscreen-symbolic"
        self.fs_btn.set_icon_name(icon)

    @property
    def item(self) -> PlayItem | None:
        return self._item

    def play(self, item: PlayItem, can_zap: bool = False) -> None:
        self.channel_overlay.hide()
        self._save_progress()
        self._stop_recording()
        self._item = item
        self._can_zap = can_zap
        self._last_save = time.monotonic()
        self.num_label.set_text(str(item.number) if item.number else "")
        self.num_label.set_visible(bool(item.number))
        set_logo(self.logo, item.logo, 34, "tv-symbolic" if item.is_live
                 else "ashy-film-symbolic", lambda: self._item is item)
        self.logo.set_visible(bool(item.logo) or item.is_live)
        self.title_label.set_text(item.title)
        for b in (self.prev_btn, self.next_btn):
            b.set_visible(can_zap)
        for b in (self.back10, self.fwd10):
            b.set_visible(not item.is_live)
        self.live_badge.set_visible(item.is_live)
        self.rec_btn.set_visible(item.is_live and item.url.startswith(("http", "rtmp")))
        self.fav_btn.set_visible(item.channel is not None and bool(item.playlist_id))
        self._paint_fav()
        self._update_info()
        self._quality = None
        if len(item.variants) > 1:
            self._quality = QualityController(
                levels=len(item.variants), index=self._start_level(item),
                auto=bool(self.settings.get("auto_quality")))
            self._quality.start(time.monotonic())
            variant = item.variants[self._quality.index]
            item.url, item.user_agent, item.referrer = (
                variant.url, variant.user_agent, variant.referrer)
        self.mpv.play(item.url, start=item.start, user_agent=item.user_agent,
                      referrer=item.referrer)
        self.play_btn.set_icon_name("media-playback-pause-symbolic")
        self._show_controls()

    def close(self) -> None:
        self._save_progress()
        self._stop_recording()
        self.mpv.stop()
        self._item = None
        self.emit("closed")

    def shutdown(self) -> None:
        self._save_progress()
        self._stop_recording()
        self.mpv.terminate()

    def detach_video(self) -> Gtk.Widget:
        """Detach the video from the player (for the mini player)."""
        self.video_holder.set_child(None)
        return self.mpv

    def attach_video(self) -> None:
        parent = self.mpv.get_parent()
        if parent is not None and parent is not self.video_holder:
            if isinstance(parent, Adw.Bin):
                parent.set_child(None)
            else:
                self.mpv.unparent()
        if self.mpv.get_parent() is None:
            self.video_holder.set_child(self.mpv)

    def handle_key(self, keyval: int, state) -> bool:
        k = keyval
        if self.channel_overlay.get_visible():
            if k in (Gdk.KEY_Escape, Gdk.KEY_Left, Gdk.KEY_BackSpace):
                self.channel_overlay.hide()
                self._show_controls()
                return True
            return False  # arrows and Enter move/choose in the channel list
        live = self._item is not None and self._item.is_live
        digit = _DIGITS.get(k)
        if digit is not None and live and self._can_zap:
            self._type_digit(digit)
            return True
        if k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_Menu) \
                and live and self._can_zap:
            self.emit("channel-list")
            return True
        if k == Gdk.KEY_BackSpace and live and self._can_zap:
            self.emit("zap-previous")
        elif k in (Gdk.KEY_i, Gdk.KEY_I):
            pass  # just show the info/controls (below)
        elif k in (Gdk.KEY_space, Gdk.KEY_k, Gdk.KEY_K, Gdk.KEY_AudioPlay):
            self._toggle_pause()
        elif k in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            self.emit("toggle-fullscreen")
        elif k in (Gdk.KEY_m, Gdk.KEY_M, Gdk.KEY_AudioMute):
            self._toggle_mute()
        elif k in (Gdk.KEY_Up, Gdk.KEY_Page_Up, Gdk.KEY_AudioPrev) and self._can_zap:
            self.emit("zap", -1)
        elif k in (Gdk.KEY_Down, Gdk.KEY_Page_Down, Gdk.KEY_AudioNext) and self._can_zap:
            self.emit("zap", 1)
        elif k == Gdk.KEY_Left and self._item:
            self.mpv.seek(-10)  # live: rewinds inside the timeshift buffer
            self._flash(f"⏪ {self._position_text()}")
        elif k == Gdk.KEY_Right and self._item:
            if live and self._behind_live() < _LIVE_EDGE_SECS:
                self._flash(_("● LIVE"))
            elif live:
                # Stay inside the seekable buffer: going past it would reopen
                # the stream from the start instead of moving forward.
                end = self.mpv.seekable_end
                target = min(self.mpv.position + 10, end - 0.5) if end else 0
                if target > self.mpv.position:
                    self.mpv.seek(target, relative=False)
                self._flash(f"⏩ {self._position_text()}")
            else:
                self.mpv.seek(10)
                self._flash(f"⏩ {self._position_text()}")
        elif k in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add, Gdk.KEY_AudioRaiseVolume):
            self._change_volume(5)
        elif k in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract, Gdk.KEY_AudioLowerVolume):
            self._change_volume(-5)
        elif k == Gdk.KEY_Escape:
            if self._digits:
                self._cancel_digits()
            elif self._fullscreen:
                self.emit("toggle-fullscreen")
            else:
                self.close()
        else:
            return False
        self._show_controls()
        return True

    # ---- mpv state -------------------------------------------------

    def _on_state(self, _mpv, state: str) -> None:
        if self._quality is not None and self._quality_event(state):
            return  # switching to another variant
        if state in ("loading", "buffering"):
            self._show_center(loading=True,
                              title=_("Loading…") if state == "loading" else _("Buffering…"))
        elif state == "error":
            self._show_center(loading=False, title=_("Could not play"),
                              desc=_("The channel may be offline or the connection dropped."))
            self._show_controls(keep=True)
        elif state == "ended":
            if self._item and not self._item.is_live:
                self._save_progress(finished=True)
                self.emit("ended", self._item)
            else:
                self._show_center(loading=False, title=_("Broadcast ended"),
                                  desc=_("The signal was interrupted."))
        else:
            self.center.set_visible(False)
        playing = state in ("playing", "buffering", "loading")
        self.play_btn.set_icon_name("media-playback-pause-symbolic" if playing
                                    else "media-playback-start-symbolic")
        if state == "paused":
            self._show_controls(keep=True)
        elif state == "playing":
            self._show_controls()

    def _show_center(self, loading: bool, title: str, desc: str = "") -> None:
        self.spinner.set_visible(loading)
        self.center_icon.set_visible(not loading)
        self.center_title.set_text(title)
        self.center_desc.set_text(desc)
        self.center_desc.set_visible(bool(desc))
        self.center_actions.set_visible(not loading)
        self.center.set_visible(True)

    def _retry(self) -> None:
        if self._item:
            item = self._item
            item.start = self.mpv.position if not item.is_live else 0
            self.play(item, self._can_zap)

    def _on_time(self, _mpv, pos: float, dur: float) -> None:
        if self._item is None:
            return
        if self._item.is_live:
            self._update_live_badge()
        if not self._item.is_live and dur > 0:
            if not self._seeking:
                self.scale.set_range(0, dur)
                self.scale.set_value(pos)
            self.pos_label.set_text(fmt_time(pos))
            self.end_label.set_text("-" + fmt_time(dur - pos))
            if time.monotonic() - self._last_save > _SAVE_EVERY:
                self._save_progress()
        if self._recording:
            self.rec_label.set_text(f"● REC {fmt_time(time.time() - self._rec_started)}")

    def _on_seek(self, _scale, _scroll, value: float) -> bool:
        if self._item is None or self._item.is_live:
            return True
        # While dragging: let the bar follow the pointer and seek only once
        # the pointer rests, instead of one HTTP range request per pixel.
        self._seeking = True
        if self._seek_source:
            GLib.source_remove(self._seek_source)
        self._seek_source = GLib.timeout_add(_SEEK_DELAY_MS, self._do_seek, value)
        self._show_controls()
        return False

    def _do_seek(self, value: float) -> bool:
        self._seek_source = 0
        self.mpv.seek(value, relative=False)
        GLib.timeout_add(500, self._end_seek)
        return GLib.SOURCE_REMOVE

    def _end_seek(self) -> bool:
        if not self._seek_source:
            self._seeking = False
        return GLib.SOURCE_REMOVE

    # ---- channel list, numbers, previous channel ---------------------

    def show_channel_list(self, channels, current: int, guide) -> None:
        # Like TiviMate: the list replaces the control bars over the video.
        if self._hide_source:
            GLib.source_remove(self._hide_source)
            self._hide_source = 0
        self.top_bar.set_reveal_child(False)
        self.bottom_bar.set_reveal_child(False)
        self.set_cursor(None)
        self.channel_overlay.show_channels(channels, current, guide)

    def _on_overlay_play(self, _overlay, index: int) -> None:
        self.channel_overlay.hide()
        self.emit("zap-to", index)

    def _type_digit(self, digit: int) -> None:
        """Type a channel number like on a TV remote: 1, 2, 3 -> channel 123."""
        if len(self._digits) >= 5:
            self._digits = ""
        self._digits += str(digit)
        self._flash(self._digits + "_", sticky=True)
        if self._digit_source:
            GLib.source_remove(self._digit_source)
        self._digit_source = GLib.timeout_add(_DIGIT_DELAY_MS, self._commit_digits)

    def _commit_digits(self) -> bool:
        self._digit_source = 0
        number, self._digits = int(self._digits or 0), ""
        self._hide_osd()
        if number > 0:
            self.emit("zap-to", number - 1)
        return GLib.SOURCE_REMOVE

    def _cancel_digits(self) -> None:
        if self._digit_source:
            GLib.source_remove(self._digit_source)
            self._digit_source = 0
        self._digits = ""
        self._hide_osd()

    # ---- timeshift ---------------------------------------------------

    def _behind_live(self) -> float:
        """Seconds between the live edge (end of the buffer) and the picture."""
        edge, pos = self.mpv.cache_time, self.mpv.position
        return max(0.0, edge - pos) if edge > 0 and pos > 0 else 0.0

    def _go_live(self) -> None:
        end = self.mpv.seekable_end
        if end > 0:
            self.mpv.seek(max(0.0, end - 0.5), relative=False)
        self.mpv.set_pause(False)
        self._flash(_("● LIVE"))
        # mpv can only seek up to slightly behind the newest data; if that is
        # still far from live, reopen the stream, which starts at the live edge.
        GLib.timeout_add(1200, self._ensure_live, self._item)

    def _ensure_live(self, item) -> bool:
        if item is self._item and item is not None and \
                self._behind_live() >= _LIVE_EDGE_SECS:
            self.mpv.play(item.url, user_agent=item.user_agent, referrer=item.referrer)
        return GLib.SOURCE_REMOVE

    def _position_text(self) -> str:
        if self._item is not None and self._item.is_live:
            behind = self._behind_live()
            return f"−{fmt_time(behind)}" if behind >= _LIVE_EDGE_SECS else _("● LIVE")
        return fmt_time(self.mpv.position)

    def _update_live_badge(self) -> None:
        behind = self._behind_live()
        shifted = behind >= _LIVE_EDGE_SECS
        self.live_badge.set_text(f"−{fmt_time(behind)}" if shifted else _("● LIVE"))
        if shifted:
            self.live_badge.add_css_class("ashy-behind")
        else:
            self.live_badge.remove_css_class("ashy-behind")
        self.go_live_btn.set_visible(shifted)

    # ---- automatic quality ---------------------------------------

    # Quality rank chosen by the last adaptive decision, shared by every
    # channel in this session: a slow connection stays slow when zapping.
    _rank_hint = 4

    def _start_level(self, item: PlayItem) -> int:
        if not self.settings.get("auto_quality"):
            return 0
        for i, variant in enumerate(item.variants):
            if _RANKS.get(variant.label, 0) <= PlayerView._rank_hint:
                return i
        return len(item.variants) - 1

    def _quality_event(self, state: str) -> bool:
        """Feed playback states to the controller; True if it switched."""
        now = time.monotonic()
        if state == "buffering":
            return self._apply_quality(self._quality.stall_started(now), "auto")
        if state == "playing":
            self._quality.stall_ended(now)
        elif state == "error":
            return self._apply_quality(self._quality.failed(now), "fallback")
        return False

    def _quality_tick(self) -> bool:
        if self._quality is not None and self.mpv.state in ("playing", "buffering"):
            full = self.mpv.cache_duration >= _BUFFER_FULL_SECS
            self._apply_quality(self._quality.tick(time.monotonic(), full), "auto")
        return GLib.SOURCE_CONTINUE

    def _apply_quality(self, index: int | None, reason: str) -> bool:
        item = self._item
        if index is None or item is None:
            return False
        variant = item.variants[index]
        if reason == "auto":
            PlayerView._rank_hint = _RANKS.get(variant.label, PlayerView._rank_hint)
        item.url, item.user_agent, item.referrer = (
            variant.url, variant.user_agent, variant.referrer)
        self.mpv.play(variant.url, user_agent=variant.user_agent, referrer=variant.referrer)
        label = variant.label or str(index + 1)
        if reason == "manual":
            self._flash(_("Quality: {label}").format(label=label))
        elif reason == "fallback":
            self._flash(_("Unavailable, switching to {label}").format(label=label))
        else:
            self._flash(_("Quality: {label} (auto)").format(label=label))
        return True

    def _choose_quality(self, value) -> None:
        if self._quality is None:
            return
        now = time.monotonic()
        if value == "auto":
            self._quality.set_auto(now)
            self.settings.set("auto_quality", True)
            self._flash(_("Automatic quality"))
            return
        if value != self._quality.index:
            self._quality.select(value, now)
            self._apply_quality(value, "manual")
        else:
            self._quality.auto = False

    # ---- EPG / info ---------------------------------------------

    def _update_info(self) -> None:
        item = self._item
        if item is None:
            return
        prog_txt, time_txt = item.subtitle, ""
        if item.is_live:
            cur = nxt = None
            if self._guide and item.tvg_id:
                cur, nxt = self._guide.now_next(item.tvg_id)
            if cur:
                prog_txt = cur.title
                time_txt = (f"{time.strftime('%H:%M', time.localtime(cur.start))} - "
                            f"{time.strftime('%H:%M', time.localtime(cur.stop))}")
                if nxt and nxt.title:
                    time_txt += "   •   " + _("Up next: {title}").format(title=nxt.title)
                self.pos_label.set_text(time.strftime("%H:%M", time.localtime(cur.start)))
                self.end_label.set_text(time.strftime("%H:%M", time.localtime(cur.stop)))
                self.scale.set_range(cur.start, cur.stop)
                self.scale.set_value(time.time())
            self.progress_row.set_visible(cur is not None)
            self.scale.set_sensitive(False)
        else:
            self.progress_row.set_visible(True)
            self.scale.set_sensitive(True)
            self.scale.set_range(0, 1)
            self.scale.set_value(0)
            self.pos_label.set_text("00:00")
            self.end_label.set_text("")
        self.prog_label.set_text(prog_txt)
        self.prog_label.set_visible(bool(prog_txt))
        self.time_label.set_text(time_txt)
        self.time_label.set_visible(bool(time_txt))

    def _refresh_epg(self) -> bool:
        if self._item and self._item.is_live and self.get_mapped():
            self._update_info()
        return GLib.SOURCE_CONTINUE

    def _save_progress(self, finished: bool = False) -> None:
        item = self._item
        if item is None or not item.playlist_id:
            return
        self._last_save = time.monotonic()
        dur = self.mpv.duration
        pos = dur if finished else self.mpv.position
        self.emit("progress", item, pos, dur)

    # ---- controls -----------------------------------------------------

    def _toggle_pause(self) -> None:
        if self.mpv.state in ("error", "ended", "idle"):
            self._retry()
            return
        playing = self.mpv.toggle_pause()
        self._flash("▶" if playing else "⏸")

    def _toggle_mute(self) -> None:
        self.mpv.muted = not self.mpv.muted
        self._update_volume_icon()
        self._flash(_("Mute") if self.mpv.muted
                    else _("Volume {level}%").format(level=int(self.mpv.volume)))

    def _change_volume(self, delta: float) -> None:
        self.mpv.muted = False
        self.mpv.volume = self.mpv.volume + delta
        self.settings.set("volume", int(self.mpv.volume))
        self._update_volume_icon()
        self._flash(_("Volume {level}%").format(level=int(self.mpv.volume)))

    def _update_volume_icon(self) -> None:
        vol = 0 if self.mpv.muted else self.mpv.volume
        icon = ("audio-volume-muted-symbolic" if vol <= 0 else
                "audio-volume-low-symbolic" if vol < 40 else
                "audio-volume-medium-symbolic" if vol < 80 else
                "audio-volume-high-symbolic")
        self.vol_btn.set_icon_name(icon)

    def _on_scroll(self, _c, _dx, dy) -> bool:
        self._change_volume(-5 if dy > 0 else 5)
        self._show_controls()
        return True

    def flash(self, text: str) -> None:
        """Short on-screen message over the video."""
        self._flash(text)

    def _flash(self, text: str, sticky: bool = False) -> None:
        self.osd.set_text(text)
        self.osd.set_visible(True)
        if self._osd_source:
            GLib.source_remove(self._osd_source)
            self._osd_source = 0
        if not sticky:
            self._osd_source = GLib.timeout_add(1200, self._hide_osd)

    def _hide_osd(self) -> bool:
        if self._osd_source:
            GLib.source_remove(self._osd_source)
        self.osd.set_visible(False)
        self._osd_source = 0
        return GLib.SOURCE_REMOVE

    def _toggle_fav(self) -> None:
        if self._item and self._item.channel is not None:
            self.emit("favorite-toggled", self._item)
            self._paint_fav()

    def _paint_fav(self) -> None:
        ch = self._item.channel if self._item else None
        active = bool(ch and ch.favorite)
        self.fav_btn.set_icon_name("ashy-heart-filled-symbolic" if active
                                   else "ashy-heart-symbolic")
        if active:
            self.fav_btn.add_css_class("active")
        else:
            self.fav_btn.remove_css_class("active")

    # ---- recording ------------------------------------------------------

    def _toggle_record(self) -> None:
        if self._recording:
            self._stop_recording()
            return
        if self._item is None:
            return
        folder = recordings_dir()
        folder.mkdir(parents=True, exist_ok=True)
        title = re.sub(r"[^\w\s.-]", "", self._item.title).strip() or _("Recording")
        prog = re.sub(r"[^\w\s.-]", "", self.prog_label.get_text()).strip()
        stem = f"{title} - {prog}" if prog else title
        # Keep well under the 255-byte file name limit (accents take 2 bytes).
        stem = stem.encode()[:120].decode(errors="ignore").strip()
        path = str(folder / f"{stem} {time.strftime('%Y-%m-%d %H-%M-%S')}.ts")
        self.mpv.record(path)
        GLib.timeout_add_seconds(5, self._check_recording, path)
        self._recording = path
        self._rec_started = time.time()
        self.rec_btn.add_css_class("recording")
        self.rec_btn.set_tooltip_text(_("Stop recording"))
        self.rec_label.set_text("● REC 00:00")
        self.rec_label.set_visible(True)
        self.emit("toast", _("Recording…"))

    def _check_recording(self, path: str) -> bool:
        if self._recording == path and not os.path.exists(path):
            self._recording = None
            self.mpv.record(None)
            self.rec_btn.remove_css_class("recording")
            self.rec_label.set_visible(False)
            self.emit("toast", _("Could not record this channel."))
        return GLib.SOURCE_REMOVE

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self.mpv.record(None)
        self._recording = None
        self.rec_btn.remove_css_class("recording")
        self.rec_btn.set_tooltip_text(_("Record"))
        self.rec_label.set_visible(False)
        self.emit("toast", _("Recording saved to Recordings."))

    # ---- popovers ------------------------------------------------------

    def _volume_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, 0, 130, 5)
        scale.set_inverted(True)
        scale.set_size_request(-1, 150)
        scale.set_value(self.mpv.volume)
        scale.connect("value-changed", lambda s: self._set_volume(s.get_value()))
        mute = Gtk.ToggleButton(icon_name="audio-volume-muted-symbolic",
                                active=self.mpv.muted, tooltip_text=_("Mute (M)"))
        mute.connect("toggled", self._on_mute_toggled)
        box.append(scale)
        box.append(mute)
        return Gtk.Popover(child=box)

    def _on_mute_toggled(self, button: Gtk.ToggleButton) -> None:
        self.mpv.muted = button.get_active()
        self._update_volume_icon()

    def _set_volume(self, value: float) -> None:
        self.mpv.muted = False
        self.mpv.volume = value
        self.settings.set("volume", int(value))
        self._update_volume_icon()

    def _radio_list(self, title: str, options, on_pick) -> Gtk.Widget:
        """options = [(label, value, active)]"""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head = Gtk.Label(label=title, xalign=0, margin_start=6, margin_top=4)
        head.add_css_class("heading")
        box.append(head)
        group = None
        for label, value, active in options:
            check = Gtk.CheckButton(label=label, active=active)
            if group is None:
                group = check
            else:
                check.set_group(group)
            check.connect("toggled", _on_radio, on_pick, value)
            box.append(check)
        return box

    def _subs_popover(self) -> Gtk.Popover:
        tracks = self.mpv.tracks("sub")
        options = [(_("Off"), None, not any(sel for _i, _l, sel in tracks))]
        options += [(label, tid, sel) for tid, label, sel in tracks]
        box = self._radio_list(_("Subtitles"), options,
                               lambda v: self.mpv.set_track("sub", v))
        if not tracks:
            note = Gtk.Label(label=_("No subtitles in this video."), margin_start=6)
            note.add_css_class("dim-label")
            box.append(note)
        return Gtk.Popover(child=box)

    def _options_popover(self) -> Gtk.Popover:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        if self._quality is not None and self._item is not None:
            q = self._quality
            options = [(_("Automatic"), "auto", q.auto)]
            options += [(v.label or str(i + 1), i, not q.auto and i == q.index)
                        for i, v in enumerate(self._item.variants)]
            box.append(self._radio_list(_("Quality"), options, self._choose_quality))
        audio = self.mpv.tracks("audio")
        if len(audio) > 1:
            box.append(self._radio_list(
                _("Audio"), [(label, tid, sel) for tid, label, sel in audio],
                lambda v: self.mpv.set_track("audio", v)))
        box.append(self._radio_list(
            _("Aspect ratio"), [(label, value, value == "no") for label, value in _ASPECTS],
            self.mpv.set_aspect))
        if self._item and not self._item.is_live:
            box.append(self._radio_list(
                _("Speed"), [(f"{s:g}x", s, s == 1.0) for s in _SPEEDS],
                self.mpv.set_speed))
        return Gtk.Popover(child=box)

    # ---- show/hide controls ------------------------------------

    def _on_motion(self, _c, x: float, y: float) -> None:
        if (x, y) == self._last_motion:
            return  # synthetic events (no real movement)
        self._last_motion = (x, y)
        self._show_controls()

    def _on_click(self, gesture, n_press: int, _x, _y) -> None:
        if gesture.get_current_button() != Gdk.BUTTON_PRIMARY:
            return
        if self._click_source:
            GLib.source_remove(self._click_source)
            self._click_source = 0
        if n_press >= 2:
            self.emit("toggle-fullscreen")
            return
        # Single click: pause (waits so it does not conflict with a double click).
        self._click_source = GLib.timeout_add(260, self._single_click)

    def _on_video_drag(self, gesture, dx: float, dy: float) -> None:
        if self._fullscreen or dx * dx + dy * dy < _DRAG_THRESHOLD ** 2:
            return
        native = self.get_native()
        surface = native.get_surface() if native else None
        if not isinstance(surface, Gdk.Toplevel):
            return
        _ok, start_x, start_y = gesture.get_start_point()
        ok, point = self.video_holder.compute_point(
            native, Graphene.Point().init(start_x, start_y))
        if not ok:
            return
        if self._click_source:  # it was a drag, not a click: don't pause
            GLib.source_remove(self._click_source)
            self._click_source = 0
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        offset_x, offset_y = native.get_surface_transform()
        surface.begin_move(gesture.get_device(), gesture.get_current_button(),
                           point.x + offset_x, point.y + offset_y,
                           gesture.get_current_event_time())
        gesture.reset()

    def _single_click(self) -> bool:
        self._click_source = 0
        self._toggle_pause()
        self._show_controls()
        return GLib.SOURCE_REMOVE

    def _show_controls(self, keep: bool = False) -> None:
        if self.channel_overlay.get_visible():
            return  # the channel list is on screen instead of the bars
        self.top_bar.set_reveal_child(True)
        self.bottom_bar.set_reveal_child(True)
        self.set_cursor(None)
        if self._hide_source:
            GLib.source_remove(self._hide_source)
            self._hide_source = 0
        if not keep:
            self._hide_source = GLib.timeout_add(_HIDE_DELAY, self._hide_controls)

    def _hide_controls(self) -> bool:
        self._hide_source = 0
        if self.mpv.state not in ("playing", "buffering"):
            return GLib.SOURCE_REMOVE
        if self.channel_overlay.get_visible() or any(
                b.get_active() for b in (self.vol_btn, self.cc_btn, self.opts_btn)):
            self._show_controls()  # popover open: wait
            return GLib.SOURCE_REMOVE
        self.top_bar.set_reveal_child(False)
        self.bottom_bar.set_reveal_child(False)
        self.set_cursor(Gdk.Cursor.new_from_name("none"))
        return GLib.SOURCE_REMOVE


def _on_radio(check: Gtk.CheckButton, on_pick, value) -> None:
    if check.get_active():
        on_pick(value)
