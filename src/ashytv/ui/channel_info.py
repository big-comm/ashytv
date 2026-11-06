"""Side panel of the TV Channels list: live preview + programme info.

The preview is a second, small libmpv player (the same GtkGLArea render
widget as the full player, created with ``preview=True``: short buffer, no
timeshift, no subtitles, muted). It is created the first time a preview
starts, so pages that never show one don't pay for it, and libmpv missing
only leaves the channel logo in its place.
"""

from __future__ import annotations

import time

from gi.repository import Gtk

from .. import compat
from ..i18n import _
from ..models import Channel
from .pages._common import set_logo


_FAINT = "ashy-faint"
_MUTED_ICON = "audio-volume-muted-symbolic"


def _hhmm(epoch: int) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


class ChannelInfoPanel(Gtk.Box):
    """Logo/preview at the top, then name, number, group, qualities, the
    current and next programme and its description."""

    __gtype_name__ = "AshyChannelInfoPanel"

    WIDTH = 340
    PREVIEW_HEIGHT = 190    # 16:9 of the panel's inner width

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("ashy-panel")
        self.add_css_class("ashy-info-panel")
        self.set_size_request(self.WIDTH, -1)
        self.set_hexpand(False)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._channel: Channel | None = None
        self._video = None            # MpvWidget, created on the first preview
        self._video_failed = False    # libmpv/GL not available
        self._muted = True
        self._hwdec = True
        self._volume = 100.0
        self._playing_url = ""

        # ---- preview area (16:9) ----
        self._stage = Gtk.Overlay(vexpand=False)
        self._stage.set_size_request(-1, self.PREVIEW_HEIGHT)
        self._stage.add_css_class("ashy-preview")
        self._stage.set_overflow(Gtk.Overflow.HIDDEN)
        self._stage.set_child(Gtk.Box())  # replaced by the video widget
        cover = Gtk.Box(halign=Gtk.Align.FILL, valign=Gtk.Align.FILL)
        cover.add_css_class("ashy-preview-cover")
        self._logo = Gtk.Image(pixel_size=96, hexpand=True, vexpand=True,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        cover.append(self._logo)
        self._cover = cover
        self._stage.add_overlay(cover)
        self._spinner = compat.spinner(width_request=28, height_request=28, visible=False,
                                    halign=Gtk.Align.START, valign=Gtk.Align.END,
                                    margin_start=10, margin_bottom=10)
        self._stage.add_overlay(self._spinner)
        self._live = Gtk.Label(label=_("Live"), visible=False, halign=Gtk.Align.START,
                               valign=Gtk.Align.START, margin_start=10, margin_top=10)
        self._live.add_css_class("ashy-preview-live")
        self._stage.add_overlay(self._live)
        self._mute_btn = Gtk.Button(icon_name=_MUTED_ICON,
                                    tooltip_text=_("Unmute"), visible=False,
                                    halign=Gtk.Align.END, valign=Gtk.Align.END,
                                    margin_end=8, margin_bottom=8)
        self._mute_btn.add_css_class("circular")
        self._mute_btn.add_css_class("osd")
        self._mute_btn.connect("clicked", lambda *_: self.toggle_mute())
        self._stage.add_overlay(self._mute_btn)
        self.append(self._stage)

        # ---- texts ----
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_margin_top(14)
        body.set_margin_bottom(18)
        # Wrapping labels ask for their whole text as natural width: cap it,
        # or the panel would grow and squeeze the channel list.
        self._name = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=3, max_width_chars=20)
        self._name.add_css_class("ashy-info-name")
        self._meta = Gtk.Label(xalign=0, wrap=True, max_width_chars=30)
        self._meta.add_css_class(_FAINT)
        self._tags = Gtk.Box(spacing=6)
        body.append(self._name)
        body.append(self._meta)
        body.append(self._tags)

        self._now_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                margin_top=8)
        self._now_box.add_css_class("ashy-info-now")
        self._now_when = Gtk.Label(xalign=0)
        self._now_when.add_css_class("ashy-info-when")
        self._now_title = Gtk.Label(xalign=0, wrap=True, max_width_chars=30)
        self._now_title.add_css_class("ashy-chan-name")
        self._now_bar = Gtk.ProgressBar()
        self._now_bar.add_css_class("ashy-progress-mini")
        self._now_left = Gtk.Label(xalign=0)
        self._now_left.add_css_class(_FAINT)
        self._desc = Gtk.Label(xalign=0, wrap=True, max_width_chars=30, margin_top=4)
        self._desc.add_css_class("ashy-info-desc")
        for w in (self._now_when, self._now_title, self._now_bar, self._now_left, self._desc):
            self._now_box.append(w)
        body.append(self._now_box)
        self._next = Gtk.Label(xalign=0, wrap=True, max_width_chars=30)
        self._next.add_css_class("ashy-info-next")
        body.append(self._next)
        self._empty = Gtk.Label(label=_("No schedule"), xalign=0)
        self._empty.add_css_class(_FAINT)
        body.append(self._empty)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_child(body)
        self.append(scroll)

    # ---- texts ---------------------------------------------------------

    @property
    def channel(self) -> Channel | None:
        return self._channel

    def show_channel(self, ch: Channel | None, number: int, guide) -> None:
        """Fill the texts (cheap: called on every selection change)."""
        changed = ch is not self._channel
        self._channel = ch
        if ch is None:
            self.stop_preview()
            self._logo.set_from_icon_name("tv-symbolic")
            for w in (self._name, self._meta, self._next, self._desc):
                w.set_text("")
            self._now_box.set_visible(False)
            self._empty.set_visible(False)
            self._clear_tags()
            return
        if changed and self._playing_url:
            self.stop_preview()  # the previous channel must not keep playing
        grouped = bool(ch.variant_labels) and bool(ch.base_name)
        self._name.set_text(ch.base_name if grouped else ch.display_name)
        meta = [_("Channel {number}").format(number=number)] if number else []
        if ch.group:
            meta.append(ch.group)
        self._meta.set_text("  •  ".join(meta))
        self._clear_tags()
        for label in ch.variant_labels:
            tag = Gtk.Label(label=label)
            tag.add_css_class("ashy-quality")
            self._tags.append(tag)
        self._tags.set_visible(bool(ch.variant_labels))
        set_logo(self._logo, ch.logo, 96, "tv-symbolic", lambda c=ch: self._channel is c)
        self.update_programme(guide)

    def _clear_tags(self) -> None:
        while (child := self._tags.get_first_child()) is not None:
            self._tags.remove(child)

    def update_programme(self, guide) -> None:
        ch = self._channel
        now = int(time.time())
        cur = nxt = None
        if ch is not None and guide and ch.tvg_id:
            cur, nxt = guide.now_next(ch.tvg_id, now)
        self._now_box.set_visible(cur is not None)
        if cur is not None:
            self._now_when.set_text(f"{_('Now')}  •  {_hhmm(cur.start)} – {_hhmm(cur.stop)}")
            self._now_title.set_text(cur.title or "—")
            self._now_bar.set_fraction(cur.progress(now))
            left = max(0, cur.stop - now) // 60
            self._now_left.set_text(_("{m} min left").format(m=left))
        self._next.set_visible(nxt is not None)
        if nxt is not None:
            self._next.set_text(f"{_hhmm(nxt.start)}  •  "
                                + _("Up next: {title}").format(title=nxt.title or "—"))
        desc = cur.desc if cur is not None else ""
        self._desc.set_text(desc)
        self._desc.set_visible(bool(desc))
        self._empty.set_visible(ch is not None and cur is None and nxt is None)

    # ---- preview -------------------------------------------------------

    def configure(self, hwdec: bool, volume: float) -> None:
        self._hwdec = hwdec
        self._volume = volume

    @property
    def previewing(self) -> bool:
        return bool(self._playing_url)

    def start_preview(self, url: str, user_agent: str = "", referrer: str = "") -> None:
        if not url or url == self._playing_url or not self._ensure_video():
            return
        self._playing_url = url
        self._video.muted = self._muted
        self._video.volume = self._volume
        self._video.play(url, user_agent=user_agent, referrer=referrer)
        self._spinner.set_visible(True)
        self._mute_btn.set_visible(True)

    def stop_preview(self) -> None:
        self._playing_url = ""
        if self._video is not None and self._video.state != "idle":
            self._video.stop()
        self._show_cover(True)
        self._spinner.set_visible(False)
        self._mute_btn.set_visible(False)

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        if self._video is not None:
            self._video.muted = self._muted
        self._mute_btn.set_icon_name(_MUTED_ICON if self._muted
                                     else "audio-volume-high-symbolic")
        self._mute_btn.set_tooltip_text(_("Unmute") if self._muted else _("Mute"))

    def shutdown(self) -> None:
        self._playing_url = ""
        if self._video is not None:
            self._video.terminate()
            self._video = None
            self._video_failed = True

    def _ensure_video(self) -> bool:
        if self._video is not None:
            return True
        if self._video_failed:
            return False
        try:
            # Deferred import: without python-mpv/libEGL the logo stays.
            from ..player.mpv_widget import MpvWidget
            self._video = MpvWidget(hwdec=self._hwdec, preview=True)
        except (ImportError, OSError, RuntimeError):
            self._video_failed = True
            return False
        self._video.set_can_target(False)
        self._video.connect("state-changed", self._on_state)
        self._stage.set_child(self._video)
        return True

    def _on_state(self, _video, state: str) -> None:
        if not self._playing_url:
            return
        playing = state in ("playing", "paused")
        self._spinner.set_visible(state in ("loading", "buffering"))
        if playing:
            self._show_cover(False)
        elif state in ("error", "ended"):
            # Offline channel: back to the logo, without retrying.
            self._show_cover(True)
            self._mute_btn.set_visible(False)

    def _show_cover(self, cover: bool) -> None:
        self._cover.set_visible(cover)
        self._live.set_visible(not cover)
