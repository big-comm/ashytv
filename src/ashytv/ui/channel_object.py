"""GObject wrapper around Channel for use in Gio.ListStore / Gtk.ListView."""

from __future__ import annotations

from gi.repository import GObject

from ..models import Channel
from ..services.text import normalize


class ChannelObject(GObject.Object):
    __gtype_name__ = "AshyChannelObject"

    def __init__(self, channel: Channel):
        super().__init__()
        self.channel = channel
        # Pre-normalized name: search does not recompute on every keystroke.
        self.key = normalize(channel.display_name)
