"""Programme reminders: "tell me when this show starts".

Pure logic (no GTK) so it can be unit tested; the window persists the list in
the preferences and checks it periodically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

LEAD_TIME = 60          # notify this many seconds before the start
FORGET_AFTER = 15 * 60  # drop reminders whose programme started long ago


@dataclass(slots=True)
class Reminder:
    playlist_id: int
    channel_url: str        # stable key of the channel
    channel_name: str
    title: str              # programme title
    start: int              # epoch seconds
    notified: bool = False

    @property
    def key(self) -> str:
        return f"{self.playlist_id}|{self.channel_url}|{self.start}"


class ReminderBook:
    def __init__(self, items: list[dict] | None = None):
        self._items: dict[str, Reminder] = {}
        for raw in items or []:
            try:
                r = Reminder(**raw)
            except TypeError:
                continue  # an entry from an older/newer format
            self._items[r.key] = r

    def __len__(self) -> int:
        return len(self._items)

    def has(self, playlist_id: int, channel_url: str, start: int) -> bool:
        return f"{playlist_id}|{channel_url}|{start}" in self._items

    def toggle(self, reminder: Reminder) -> bool:
        """Add or remove; returns True when the reminder is now set."""
        if reminder.key in self._items:
            del self._items[reminder.key]
            return False
        self._items[reminder.key] = reminder
        return True

    def due(self, now: float) -> list[Reminder]:
        """Reminders to announce now (marked as notified)."""
        out = []
        for r in self._items.values():
            if not r.notified and r.start - LEAD_TIME <= now < r.start + FORGET_AFTER:
                r.notified = True
                out.append(r)
        return out

    def prune(self, now: float) -> bool:
        """Forget old reminders; True if something was removed."""
        old = [k for k, r in self._items.items() if now >= r.start + FORGET_AFTER]
        for k in old:
            del self._items[k]
        return bool(old)

    def to_list(self) -> list[dict]:
        return [asdict(r) for r in sorted(self._items.values(), key=lambda r: r.start)]
