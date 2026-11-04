"""Speed test group of Settings → Network (services/speedtest.py does the work)."""

from __future__ import annotations

import threading

from gi.repository import Adw, GLib, Gtk

from ..i18n import _
from ..services import free_tv, speedtest
from ..services.speedtest import Verdict
from ..services.xtream_client import XtreamProvider

_VERDICTS = {
    Verdict.UHD: _("Good for 4K"),
    Verdict.FHD: _("Good for Full HD"),
    Verdict.HD: _("Good for HD"),
    Verdict.SD: _("Only SD quality"),
    Verdict.LOW: _("Too slow for video"),
}


def playlist_servers(storage) -> list[tuple[str, str]]:
    """(playlist name, server root) of the enabled playlists served from the
    network, on the server in use (files and Free TV have none)."""
    out = []
    for row in storage.list_playlists():
        if not row["enabled"]:
            continue
        if row["kind"] == "xtream":
            try:
                main = XtreamProvider.from_json(row["source"], "").host
            except (ValueError, KeyError):
                continue
        elif "://" in row["source"] and not free_tv.is_free_tv(row["source"]):
            main = row["source"]
        else:
            continue
        servers = [main, *storage.backups(int(row["id"]))]
        active = row["active_server"] or 0
        root = speedtest.server_root(servers[active] if active < len(servers) else main)
        if root:
            out.append((row["name"], root))
    return out


class SpeedTestGroup(Adw.PreferencesGroup):
    __gtype_name__ = "AshySpeedTestGroup"

    def __init__(self, storage):
        super().__init__(
            title=_("Speed test"),
            description=_("Measures your internet (Cloudflare) and how fast each "
                          "playlist's server answers. Takes about 15 seconds."))
        self.storage = storage
        self._cancel: threading.Event | None = None
        self._meter: speedtest.Meter | None = None
        self._tick = 0

        self.button = Gtk.Button(label=_("Start test"), valign=Gtk.Align.CENTER)
        self.button.add_css_class("suggested-action")
        self.button.connect("clicked", self._on_button)
        self.set_header_suffix(self.button)

        self.speed_row = Adw.ActionRow(title=_("Download"), subtitle=_("Not measured yet"))
        self.speed_row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic"))
        self.speed_value = Gtk.Label(label="—", valign=Gtk.Align.CENTER)
        self.speed_value.add_css_class("ashy-speed-value")
        self.speed_value.add_css_class("numeric")
        self.speed_row.add_suffix(self.speed_value)
        self.add(self.speed_row)

        self.latency_row = Adw.ActionRow(title=_("Internet response time"), subtitle="—")
        self.latency_row.add_prefix(Gtk.Image(icon_name="network-transmit-receive-symbolic"))
        self.add(self.latency_row)

        self.servers_row = Adw.ExpanderRow(title=_("Playlist servers"), visible=False)
        self.servers_row.add_prefix(Gtk.Image(icon_name="network-server-symbolic"))
        self.add(self.servers_row)
        self._server_rows: list[Adw.ActionRow] = []

        self.connect("unmap", lambda *_: self.stop())

    # ---- control -------------------------------------------------------

    def _on_button(self, _b) -> None:
        if self._cancel is not None:
            self.stop()
        else:
            self.start()

    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        self._finish()

    def _finish(self) -> None:
        self._cancel = None
        self._meter = None
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = 0
        self.button.set_label(_("Start test"))
        self.button.add_css_class("suggested-action")

    def start(self) -> None:
        cancel = self._cancel = threading.Event()
        meter = self._meter = speedtest.Meter()
        servers = playlist_servers(self.storage)
        self.button.set_label(_("Stop"))
        self.button.remove_css_class("suggested-action")
        self.speed_value.set_text("—")
        self.speed_row.set_subtitle(_("Measuring…"))
        self.latency_row.set_subtitle(_("Measuring…"))
        for row in self._server_rows:
            self.servers_row.remove(row)
        self._server_rows = []
        for name, _url in servers:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=_("Measuring…"))
            self.servers_row.add_row(row)
            self._server_rows.append(row)
        self.servers_row.set_visible(bool(servers))
        self.servers_row.set_subtitle("")
        self._tick = GLib.timeout_add(250, self._on_tick, meter)

        def worker():
            ms = speedtest.latency_ms(speedtest.LATENCY_URL)
            self._post(cancel, self._show_latency, ms)
            result = speedtest.download_mbps(meter, cancel)
            self._post(cancel, self._show_speed, result)
            times = []
            for i, (_name, url) in enumerate(servers):
                if cancel.is_set():
                    return
                t = speedtest.latency_ms(url, tries=3)
                times.append(t)
                self._post(cancel, self._show_server, i, t)
            self._post(cancel, self._done, times)

        threading.Thread(target=worker, daemon=True, name="ashytv-speedtest").start()

    def _post(self, cancel: threading.Event, func, *args) -> None:
        def run():
            if not cancel.is_set() and cancel is self._cancel:
                func(*args)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(run)

    # ---- results -------------------------------------------------------

    def _on_tick(self, meter) -> bool:
        if meter is not self._meter or not meter.bytes:
            return GLib.SOURCE_CONTINUE if meter is self._meter else GLib.SOURCE_REMOVE
        self.speed_value.set_text(_("{n} Mbps").format(n=f"{meter.mbps():.0f}"))
        return GLib.SOURCE_CONTINUE

    def _show_latency(self, ms) -> None:
        self.latency_row.set_subtitle(_("{n} ms").format(n=f"{ms:.0f}") if ms is not None
                                      else _("No answer"))

    def _show_speed(self, value) -> None:
        if self._tick:
            GLib.source_remove(self._tick)
            self._tick = 0
        if value is None:
            self.speed_value.set_text("—")
            self.speed_row.set_subtitle(_("Could not download the test file. "
                                          "Check your connection."))
            return
        self.speed_value.set_text(_("{n} Mbps").format(n=f"{value:.0f}"))
        self.speed_row.set_subtitle(_VERDICTS[speedtest.verdict(value)])

    def _show_server(self, index: int, ms) -> None:
        if index >= len(self._server_rows):
            return
        row = self._server_rows[index]
        if ms is None:
            row.set_subtitle(_("No answer — the server may be down"))
        elif speedtest.server_is_slow(ms):
            row.set_subtitle(_("{n} ms — slow: channels may stall even on a fast "
                               "connection").format(n=f"{ms:.0f}"))
        else:
            row.set_subtitle(_("{n} ms").format(n=f"{ms:.0f}"))
        if speedtest.server_is_slow(ms):
            row.add_css_class("warning")

    def _done(self, times: list) -> None:
        slow = sum(1 for t in times if speedtest.server_is_slow(t))
        if times:
            self.servers_row.set_subtitle(
                _("Some playlist servers are slow or silent") if slow
                else _("All answer quickly"))
            self.servers_row.set_expanded(bool(slow) or len(times) <= 3)
        self._finish()
