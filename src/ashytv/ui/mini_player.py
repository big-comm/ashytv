"""Mini player (picture-in-picture): small window with the video."""

from __future__ import annotations

from gi.repository import Adw, GObject, Gtk

from ..i18n import _


class MiniPlayer(Adw.Window):
    __gtype_name__ = "AshyMiniPlayer"

    __gsignals__ = {
        "expand": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, video: Gtk.Widget, title: str, **kwargs):
        super().__init__(title=f"{title} — AshyTV", default_width=480,
                         default_height=270, **kwargs)
        self.add_css_class("ashy-player")
        self._expanding = False

        self.holder = Adw.Bin(child=video)
        overlay = Gtk.Overlay()
        # WindowHandle: drag the window by clicking on the video.
        overlay.set_child(Gtk.WindowHandle(child=self.holder))

        bar = Gtk.Box(spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.START,
                      margin_top=8, margin_end=8)
        expand = Gtk.Button(icon_name="view-fullscreen-symbolic",
                            tooltip_text=_("Back to AshyTV"))
        expand.add_css_class("circular")
        expand.add_css_class("osd")
        expand.connect("clicked", lambda *_: self._expand())
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Stop"))
        close.add_css_class("circular")
        close.add_css_class("osd")
        close.connect("clicked", lambda *_: self.close())
        bar.append(expand)
        bar.append(close)
        self.revealer = Gtk.Revealer(child=bar, reveal_child=False,
                                     transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                     halign=Gtk.Align.END, valign=Gtk.Align.START)
        overlay.add_overlay(self.revealer)
        self.set_content(overlay)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.revealer.set_reveal_child(True))
        motion.connect("leave", lambda *_: self.revealer.set_reveal_child(False))
        overlay.add_controller(motion)
        self.connect("close-request", self._on_close)

    def release_video(self) -> None:
        self.holder.set_child(None)

    def _expand(self) -> None:
        self._expanding = True
        self.emit("expand")
        self.close()

    def _on_close(self, _w) -> bool:
        if not self._expanding:
            self.emit("stop")
        return False
