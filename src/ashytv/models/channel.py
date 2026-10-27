"""AshyTV data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..i18n import _


class MediaType(str, Enum):
    """Content type derived from M3U/Xtream."""

    LIVE = "live"
    MOVIE = "movie"
    SERIES = "series"

    @property
    def label(self) -> str:
        return {
            MediaType.LIVE: _("Live TV"),
            MediaType.MOVIE: _("Movies"),
            MediaType.SERIES: _("Series"),
        }[self]


@dataclass(slots=True)
class Channel:
    """A playlist item: live channel, movie or series.

    For series, `url` is a stable key (e.g. ``series/123``) and the episodes
    live in `Episode`. For Xtream providers, `url` holds only the stream path
    (e.g. ``live/560.ts``) — credentials are added at play time.
    """

    name: str
    url: str
    media_type: MediaType = MediaType.LIVE
    group: str = ""
    tvg_id: str = ""        # links to the EPG (XMLTV)
    tvg_name: str = ""
    logo: str = ""          # logo/poster URL
    playlist_id: int = 0
    id: int = 0
    favorite: bool = False
    ext_id: str = ""        # provider id (stream_id / series_id)
    rating: float = 0.0     # rating 0–10
    year: str = ""
    plot: str = ""
    genre: str = ""
    backdrop: str = ""
    added: int = 0          # epoch when the provider added it
    user_agent: str = ""    # #EXTVLCOPT:http-user-agent
    referrer: str = ""      # #EXTVLCOPT:http-referrer
    quality: int = 0        # live variants: 4=4K 3=FHD 2=HD 1=SD 0=unknown
    base_name: str = ""     # name without the quality suffix
    # Runtime only: quality labels of the grouped variants ("FHD", "HD"…).
    variant_labels: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return self.name or self.tvg_name or _("Unnamed")


@dataclass(slots=True)
class Episode:
    """An episode of a series."""

    season: int
    episode: int
    title: str
    url: str
    logo: str = ""
    plot: str = ""
    duration: int = 0       # seconds
    id: int = 0

    @property
    def label(self) -> str:
        return _("S{season} • E{episode}").format(season=self.season, episode=self.episode)


@dataclass(slots=True)
class Variant:
    """One quality of a live channel, ready to play."""

    label: str              # "FHD", "HD"… ("" when unknown)
    url: str
    user_agent: str = ""
    referrer: str = ""


@dataclass(slots=True)
class PlayItem:
    """What the player is playing (channel, movie, episode or recording)."""

    title: str
    url: str                # final URL (with credentials, if Xtream)
    key: str                # stable key (history/favorites)
    media_type: MediaType = MediaType.LIVE
    playlist_id: int = 0
    subtitle: str = ""
    logo: str = ""
    tvg_id: str = ""
    number: int = 0
    start: float = 0.0      # resume position (s)
    channel: Channel | None = None
    user_agent: str = ""
    referrer: str = ""
    variants: list[Variant] = field(default_factory=list)  # best quality first
    sub_files: list[str] = field(default_factory=list)  # provider subtitle URLs

    @property
    def is_live(self) -> bool:
        return self.media_type is MediaType.LIVE
