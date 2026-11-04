"""What Home shows, like streaming apps: a featured box and poster rails.

GTK-free (unit tested): the page asks the database and lays out the result.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Channel, MediaType

HERO_COUNT = 5          # titles taking turns in the featured box
HERO_MOVIES = 3
HERO_SERIES = 2
RAIL_TITLES = 20        # posters per rail, then a "See more" card
GROUP_RAILS = 10        # rails made of the playlist's groups
MIN_GROUP_TITLES = 4    # smaller groups get no rail of their own


def featured(movies: Sequence[Channel], series: Sequence[Channel],
             count: int = HERO_COUNT) -> list[Channel]:
    """The newest titles that have a picture, movies and series in turns."""
    m = [c for c in movies if c.logo or c.backdrop][:HERO_MOVIES]
    s = [c for c in series if c.logo or c.backdrop][:HERO_SERIES]
    out: list[Channel] = []
    for i in range(max(len(m), len(s))):
        out.extend(x[i] for x in (m, s) if i < len(x))
    return out[:count]


def group_rails(movie_groups: Sequence[tuple[str, int]],
                series_groups: Sequence[tuple[str, int]],
                limit: int = GROUP_RAILS) -> list[tuple[MediaType, str, int]]:
    """(type, group, count) of the rails: the biggest groups first, movies and
    series mixed, groups with fewer than MIN_GROUP_TITLES titles left out."""
    groups = [(MediaType.MOVIE, g, n) for g, n in movie_groups]
    groups += [(MediaType.SERIES, g, n) for g, n in series_groups]
    groups = [x for x in groups if x[2] >= MIN_GROUP_TITLES]
    groups.sort(key=lambda x: x[2], reverse=True)  # stable: provider order on ties
    return groups[:limit]


def rail(items: Sequence[Channel], limit: int = RAIL_TITLES) -> tuple[list[Channel], bool]:
    """The posters a rail shows and whether a "See more" card follows them
    (pass one more item than `limit` to know)."""
    return list(items[:limit]), len(items) > limit
