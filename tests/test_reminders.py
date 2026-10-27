"""Programme reminders."""

from ashytv.services.reminders import FORGET_AFTER, LEAD_TIME, Reminder, ReminderBook


def _r(start=1000, url="live/1.ts"):
    return Reminder(playlist_id=1, channel_url=url, channel_name="News", title="Show",
                    start=start)


def test_toggle_and_persist():
    book = ReminderBook()
    assert book.toggle(_r()) is True
    assert book.has(1, "live/1.ts", 1000)
    restored = ReminderBook(book.to_list())
    assert len(restored) == 1
    assert restored.toggle(_r()) is False  # toggling again removes it
    assert len(restored) == 0


def test_due_once_shortly_before_start():
    book = ReminderBook()
    book.toggle(_r(start=1000))
    assert book.due(1000 - LEAD_TIME - 1) == []
    assert [r.title for r in book.due(1000 - LEAD_TIME)] == ["Show"]
    assert book.due(1000) == []  # announced only once


def test_prune_forgets_old_and_ignores_bad_entries():
    stored = {"playlist_id": 1, "channel_url": "a", "channel_name": "A", "title": "T",
              "start": 1000}
    book = ReminderBook([stored, {"unexpected": True}])
    assert len(book) == 1
    assert book.prune(1000 + FORGET_AFTER) is True
    assert len(book) == 0
