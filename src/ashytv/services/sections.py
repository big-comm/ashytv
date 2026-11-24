"""Which of the three kinds of content the app shows at all.

A playlist from a provider is usually all three — live channels, films and
series — but plenty of people only ever use one of them. Switching the other
two off takes their entries out of the menu rather than leaving them there to
open onto nothing.

This is a global preference, not a per-playlist one: it says how the person
uses the app, not what a given list contains. Hiding a group or a channel
inside a list is a different thing and lives in the database (hidden table).

GTK-free so the rules can be unit tested. Port of web/src/core/sections.ts and
androidtv .../core/Sections.kt — keep the three in step.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import MediaType

SECTIONS: tuple[MediaType, ...] = (MediaType.LIVE, MediaType.MOVIE, MediaType.SERIES)

#: Pages that exist only to show one kind of content.
PAGE_OF: dict[MediaType, str] = {
    MediaType.LIVE: "channels",
    MediaType.MOVIE: "movies",
    MediaType.SERIES: "series",
}


def parse(stored: Iterable | None) -> set[MediaType]:
    """What came out of settings.json, made safe.

    Unknown names are dropped, and all three switched off reads as none: that
    state cannot be reached through the screens and would leave the app with
    nothing to open.
    """
    out: set[MediaType] = set()
    for name in stored or ():
        try:
            out.add(MediaType(name))
        except ValueError:
            continue
    return set() if len(out) >= len(SECTIONS) else out


def names(hidden: Iterable[MediaType]) -> list[str]:
    """Back to settings.json, in a fixed order so the file stops churning."""
    # Materialised first: `hidden` may be a one-shot iterator, and building
    # the set inside the comprehension would consume it on the first element.
    off = set(hidden)
    return [t.value for t in SECTIONS if t in off]


def hidden(off: set[MediaType], media_type: MediaType) -> bool:
    return media_type in off


def can_hide(off: set[MediaType], media_type: MediaType) -> bool:
    """Switching the last visible section off is refused.

    Nothing would be left to open, and the person would have to guess that
    Settings is where to undo it. Switching one back on is always allowed.
    """
    return media_type in off or len(off) < len(SECTIONS) - 1


def toggle(off: set[MediaType], media_type: MediaType, hide: bool) -> set[MediaType]:
    if not hide:
        return off - {media_type}
    if not can_hide(off, media_type):
        return set(off)
    return off | {media_type}


def home_hidden(off: set[MediaType]) -> bool:
    """Home is built from films and series.

    The featured banner and every poster rail come from the two catalogs, so
    with neither of them there is no Home to show — not an empty one, none.
    The entry goes and the app opens on Channels instead, which is what a
    live-only IPTV app looks like. A banner made of channel logos would be
    small squares where a wide backdrop belongs.
    """
    return MediaType.MOVIE in off and MediaType.SERIES in off


def favorites_hidden(off: set[MediaType]) -> bool:
    """Favorites is mostly films and series, so it follows Home."""
    return home_hidden(off)


def page_hidden(page: str, off: set[MediaType]) -> bool:
    """True when the section behind a page key is switched off."""
    if page == "home":
        return home_hidden(off)
    if page == "favorites":
        return favorites_hidden(off)
    for media_type in SECTIONS:
        if page == PAGE_OF[media_type]:
            return media_type in off
    # Recordings, Playlists, Search and Settings have no section of their own.
    # Recordings in particular stays: those files are already on the disk, and
    # switching Live TV off is not a reason to hide what was recorded from it.
    return False


def visible_pages(pages: Iterable[str], off: set[MediaType]) -> list[str]:
    """Keeps the order of `pages`, dropping the ones with nothing behind them."""
    return [p for p in pages if not page_hidden(p, off)]


def default_page(off: set[MediaType]) -> str:
    """Where the app opens, and where it lands when the current page goes.

    There is no third case: Home survives while either catalog does, so once
    it goes the only content left is Live — and `can_hide` is what guarantees
    Live was not switched off as well. The last line is that guarantee
    failing, not a state the screens can produce.
    """
    if not home_hidden(off):
        return "home"
    return "settings" if MediaType.LIVE in off else "channels"
