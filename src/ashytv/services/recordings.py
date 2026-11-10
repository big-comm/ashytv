"""Scheduled recordings: "record this programme for me".

Pure logic (no GTK, no network) so it can be unit tested; the window keeps
the list in the preferences and checks it periodically, as it does with
reminders.

A scheduled recording does not go through the player. The player records
what is on screen, and a recording that waits for ten o'clock has to happen
whether or not anyone is watching — and without dragging the viewer off the
channel they chose. So it opens the channel on its own (services/recorder.py)
and writes the stream to a file.

That costs one connection of the provider's account, which is the thing most
likely to surprise someone: an account with a single connection cannot watch
and record at the same time. The window says so when a recording starts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# The guide's times are not exact, and a programme rarely starts on the
# second. Record a little around it, as a recorder with a tuner would.
LEAD_IN = 60        # start this many seconds early
RUN_OVER = 120      # keep going this long after the listed end
FORGET_AFTER = 7 * 24 * 3600  # drop finished entries after a week

WAITING, RECORDING, DONE, FAILED = "waiting", "recording", "done", "failed"


@dataclass(slots=True)
class Scheduled:
    playlist_id: int
    channel_url: str        # stable key of the channel, and what is recorded
    channel_name: str
    title: str              # programme title
    start: int              # epoch seconds, as the guide lists it
    stop: int               # epoch seconds, as the guide lists it
    state: str = WAITING
    path: str = ""          # the file, once it exists
    error: str = ""

    @property
    def key(self) -> str:
        return f"{self.playlist_id}|{self.channel_url}|{self.start}"

    @property
    def begins(self) -> int:
        return self.start - LEAD_IN

    @property
    def ends(self) -> int:
        return self.stop + RUN_OVER

    def overlaps(self, other: Scheduled) -> bool:
        return self.begins < other.ends and other.begins < self.ends


class RecordingBook:
    """The scheduled recordings, by key."""

    def __init__(self, items: list[dict] | None = None):
        self._items: dict[str, Scheduled] = {}
        for raw in items or []:
            try:
                s = Scheduled(**raw)
            except TypeError:
                continue  # an entry from an older/newer format
            self._items[s.key] = s

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(sorted(self._items.values(), key=lambda s: s.start))

    def has(self, playlist_id: int, channel_url: str, start: int) -> bool:
        return f"{playlist_id}|{channel_url}|{start}" in self._items

    def get(self, key: str) -> Scheduled | None:
        return self._items.get(key)

    def toggle(self, s: Scheduled) -> bool:
        """Add or remove; returns True when the recording is now scheduled.

        A recording already running is not dropped by accident: cancel takes
        it out of the list, and the window stops the worker it belongs to.
        """
        if s.key in self._items:
            del self._items[s.key]
            return False
        self._items[s.key] = s
        return True

    def remove(self, key: str) -> None:
        self._items.pop(key, None)

    def clashes(self, s: Scheduled) -> list[Scheduled]:
        """Scheduled recordings whose time overlaps this one's.

        Not a refusal — two recordings are fine when the account has the
        connections for them. It is what the window warns about.
        """
        return [o for o in self._items.values()
                if o.key != s.key and o.state in (WAITING, RECORDING) and s.overlaps(o)]

    def due(self, now: float) -> list[Scheduled]:
        """Recordings whose time has come and that are not running yet."""
        return [s for s in self._items.values()
                if s.state == WAITING and s.begins <= now < s.ends]

    def over(self, now: float) -> list[Scheduled]:
        """Recordings running whose time is up."""
        return [s for s in self._items.values() if s.state == RECORDING and now >= s.ends]

    def missed(self, now: float) -> list[Scheduled]:
        """Waiting recordings whose programme has already ended — the app was
        closed through it. They are marked failed rather than started late."""
        return [s for s in self._items.values() if s.state == WAITING and now >= s.ends]

    def prune(self, now: float) -> bool:
        """Drop what finished long ago. True when something was dropped."""
        old = [k for k, s in self._items.items()
               if s.state in (DONE, FAILED) and now - s.ends > FORGET_AFTER]
        for k in old:
            del self._items[k]
        return bool(old)

    def to_list(self) -> list[dict]:
        return [asdict(s) for s in self]
