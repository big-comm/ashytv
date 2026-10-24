"""Video widget: libmpv embedded in a GtkGLArea (render API).

This module is imported on demand (only when playing something), so that a
missing python-mpv/libEGL doesn't prevent the app from opening.
"""

from __future__ import annotations

import ctypes
import locale

import mpv
from gi.repository import GLib, GObject, Gtk

from ..i18n import _

# Resolve OpenGL function pointers via EGL (GTK4 uses EGL on Wayland and, by
# default, on X11 too). If libEGL is missing, try GLX.
try:
    _egl = ctypes.CDLL("libEGL.so.1")
    _egl.eglGetProcAddress.restype = ctypes.c_void_p
    _egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
    _GET_PROC = _egl.eglGetProcAddress
except OSError:  # pragma: no cover — system dependent
    _glx = ctypes.CDLL("libGL.so.1")
    _glx.glXGetProcAddressARB.restype = ctypes.c_void_p
    _glx.glXGetProcAddressARB.argtypes = [ctypes.c_char_p]
    _GET_PROC = _glx.glXGetProcAddressARB


def _proc(name: str) -> int:
    return _GET_PROC(name.encode("utf-8")) or 0


def _get_proc_address(_ctx, name) -> int:
    if isinstance(name, bytes):
        name = name.decode("utf-8")
    return _proc(name)


# python-mpv requires a ctypes callback (not a plain Python function).
_PROC_ADDRESS_CB = mpv.MpvGlGetProcAddressFn(_get_proc_address)


# glGetIntegerv resolved to read the current GtkGLArea framebuffer.
_GL_DRAW_FRAMEBUFFER_BINDING = 0x8CA6
_glGetIntegerv_t = ctypes.CFUNCTYPE(
    None, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)
)
_glGetIntegerv = None


def _current_fbo() -> int:
    global _glGetIntegerv
    if _glGetIntegerv is None:
        _glGetIntegerv = _glGetIntegerv_t(_proc("glGetIntegerv"))
    value = ctypes.c_int(0)
    _glGetIntegerv(_GL_DRAW_FRAMEBUFFER_BINDING, ctypes.byref(value))
    return value.value


class MpvWidget(Gtk.GLArea):
    """OpenGL area that renders mpv's video."""

    __gtype_name__ = "AshyMpvWidget"

    __gsignals__ = {
        # loading | playing | paused | buffering | error | ended
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # position, duration (s) — duration 0 for live streams
        "time-changed": (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
        "tracks-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, hwdec: bool = True, cache_secs: int = 20):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_has_depth_buffer(False)
        self.set_has_stencil_buffer(False)

        # libmpv requires LC_NUMERIC=C to parse numbers correctly.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self._mpv = mpv.MPV(
            vo="libmpv",
            hwdec="auto-safe" if hwdec else "no",
            ytdl=False,
            input_default_bindings=False,
            osc=False,
            cache="yes",
            cache_secs=str(cache_secs),
            demuxer_max_bytes="64MiB",
            demuxer_max_back_bytes="16MiB",
            network_timeout="20",
            user_agent="AshyTV",
        )
        self._ctx: mpv.MpvRenderContext | None = None
        self._state = "idle"
        self._duration = 0.0
        self._render_pending = False
        self._terminated = False

        self._mpv.register_event_callback(self._on_mpv_event)
        for prop in ("pause", "paused-for-cache", "duration"):
            self._mpv.observe_property(prop, self._on_mpv_property)
        self._mpv.observe_property("time-pos", self._on_time_pos)
        self._mpv.observe_property("track-list", self._on_tracks)

        self.connect("realize", self._on_realize)
        self.connect("unrealize", self._on_unrealize)
        self.connect("render", self._on_render)

    # ---- GL lifecycle -------------------------------------------------

    def _on_realize(self, _area) -> None:
        self.make_current()
        if self.get_error() is not None or self._terminated:
            return
        self._ctx = mpv.MpvRenderContext(
            self._mpv,
            "opengl",
            opengl_init_params={"get_proc_address": _PROC_ADDRESS_CB},
        )
        self._ctx.update_cb = self._on_mpv_update

    def _on_unrealize(self, _area) -> None:
        # Only frees the render context: the widget may be realized again
        # (e.g. when moving to the mini player window). mpv stays alive.
        if self._ctx is not None:
            self.make_current()
            self._ctx.free()
            self._ctx = None

    def terminate(self) -> None:
        """Shut down mpv (call when closing the app)."""
        if self._terminated:
            return
        self._terminated = True
        if self._ctx is not None:
            self._ctx.free()
            self._ctx = None
        self._mpv.terminate()

    def _on_mpv_update(self) -> None:
        # Called from an mpv thread — schedules a single redraw on the main thread.
        if not self._render_pending:
            self._render_pending = True
            GLib.idle_add(self._queue)

    def _queue(self) -> bool:
        self._render_pending = False
        self.queue_render()
        return GLib.SOURCE_REMOVE

    def _on_render(self, _area, _gl_ctx) -> bool:
        if self._ctx is None:
            return True
        scale = self.get_scale_factor()
        self._ctx.render(
            flip_y=True,
            opengl_fbo={"w": self.get_width() * scale,
                        "h": self.get_height() * scale,
                        "fbo": _current_fbo()},
        )
        return True

    # ---- mpv events (mpv thread -> main thread) ------------------------

    def _on_mpv_event(self, event) -> None:
        eid = event.event_id.value
        if eid == mpv.MpvEventID.START_FILE:
            GLib.idle_add(self._set_state, "loading")
        elif eid == mpv.MpvEventID.FILE_LOADED:
            GLib.idle_add(self._set_state, "paused" if self._mpv_pause() else "playing")
        elif eid == mpv.MpvEventID.END_FILE:
            reason = event.data.reason
            if reason == mpv.MpvEventEndFile.ERROR:
                GLib.idle_add(self._set_state, "error")
            elif reason == mpv.MpvEventEndFile.EOF:
                GLib.idle_add(self._set_state, "ended")

    def _mpv_pause(self) -> bool:
        try:
            return bool(self._mpv.pause)
        except Exception:
            return False

    def _on_mpv_property(self, name, value) -> None:
        GLib.idle_add(self._property_main, name, value)

    def _property_main(self, name, value) -> bool:
        if name == "duration":
            self._duration = float(value or 0)
        elif name == "pause" and self._state in ("playing", "paused", "buffering"):
            self._set_state("paused" if value else "playing")
        elif name == "paused-for-cache" and self._state in ("playing", "buffering"):
            self._set_state("buffering" if value else "playing")
        return GLib.SOURCE_REMOVE

    def _on_time_pos(self, _name, value) -> None:
        if value is not None:
            GLib.idle_add(self._emit_time, float(value))

    def _emit_time(self, pos: float) -> bool:
        self.emit("time-changed", pos, self._duration)
        return GLib.SOURCE_REMOVE

    def _on_tracks(self, _name, _value) -> None:
        GLib.idle_add(self._emit_tracks)

    def _emit_tracks(self) -> bool:
        self.emit("tracks-changed")
        return GLib.SOURCE_REMOVE

    def _set_state(self, state: str) -> bool:
        if state != self._state:
            self._state = state
            self.emit("state-changed", state)
        return GLib.SOURCE_REMOVE

    # ---- control -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def play(self, url: str, start: float = 0, user_agent: str = "",
             referrer: str = "") -> None:
        options = {}
        if start > 0:
            options["start"] = f"+{start:.0f}"
        if user_agent:
            options["user_agent"] = user_agent
        if referrer:
            options["referrer"] = referrer
        self._duration = 0.0
        self._set_state("loading")
        self._mpv.loadfile(url, "replace", **options)
        self._mpv.pause = False

    def stop(self) -> None:
        try:
            self._mpv.command("stop")
        except Exception:
            pass
        self._set_state("idle")

    def set_pause(self, value: bool) -> None:
        self._mpv.pause = value

    def toggle_pause(self) -> bool:
        """Toggle pause; returns True if it is now playing."""
        self._mpv.pause = not self._mpv.pause
        return not self._mpv.pause

    def seek(self, seconds: float, relative: bool = True) -> None:
        try:
            self._mpv.seek(seconds, "relative" if relative else "absolute")
        except Exception:
            pass

    @property
    def position(self) -> float:
        try:
            return float(self._mpv.time_pos or 0)
        except Exception:
            return 0.0

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def volume(self) -> float:
        return float(self._mpv.volume or 0)

    @volume.setter
    def volume(self, value: float) -> None:
        self._mpv.volume = max(0.0, min(130.0, value))

    @property
    def muted(self) -> bool:
        return bool(self._mpv.mute)

    @muted.setter
    def muted(self, value: bool) -> None:
        self._mpv.mute = value

    def tracks(self, kind: str) -> list[tuple[int, str, bool]]:
        """Tracks of 'audio' or 'sub': (id, label, selected)."""
        out = []
        for t in self._mpv.track_list or []:
            if t.get("type") != kind:
                continue
            parts = [t.get("title") or "", t.get("lang") or ""]
            label = " — ".join(p for p in parts if p) or _("Track {number}").format(
                number=t.get("id"))
            out.append((int(t["id"]), label, bool(t.get("selected"))))
        return out

    def set_track(self, kind: str, track_id: int | None) -> None:
        prop = "aid" if kind == "audio" else "sid"
        self._mpv[prop] = "no" if track_id is None else track_id

    def set_aspect(self, value: str) -> None:
        self._mpv["video-aspect-override"] = value

    def set_speed(self, value: float) -> None:
        self._mpv.speed = value

    def record(self, path: str | None) -> None:
        """Record the current stream to `path` (None to stop)."""
        self._mpv["stream-record"] = path or ""
