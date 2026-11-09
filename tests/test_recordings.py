"""Scheduled recordings (services/recordings.py and services/recorder.py)."""

import http.server
import threading
import time

from ashytv.services.recorder import Recording, safe_name
from ashytv.services.recordings import (
    DONE, LEAD_IN, RECORDING, RUN_OVER, WAITING,
    RecordingBook, Scheduled,
)


def _rec(start, stop, url="u/1", pid=1, **kw):
    return Scheduled(playlist_id=pid, channel_url=url, channel_name="Canal",
                     title="Jornal", start=start, stop=stop, **kw)


def test_a_recording_starts_early_and_ends_late():
    # The guide's times are not exact and programmes rarely start on the
    # second, so the recording wraps around what is listed.
    s = _rec(1000, 2000)
    assert s.begins == 1000 - LEAD_IN
    assert s.ends == 2000 + RUN_OVER


def test_scheduling_the_same_programme_twice_cancels_it():
    book = RecordingBook()
    s = _rec(1000, 2000)
    assert book.toggle(s) is True
    assert len(book) == 1
    assert book.has(1, "u/1", 1000)
    assert book.toggle(_rec(1000, 2000)) is False
    assert len(book) == 0


def test_only_what_is_due_starts():
    book = RecordingBook()
    book.toggle(_rec(1000, 2000, url="a"))
    book.toggle(_rec(5000, 6000, url="b"))
    assert [s.channel_url for s in book.due(1000 - LEAD_IN)] == ["a"]
    assert book.due(500) == []
    # Already running: it must not be started a second time.
    book.get("1|a|1000").state = RECORDING
    assert book.due(1000) == []


def test_a_recording_running_is_stopped_when_its_time_is_up():
    book = RecordingBook()
    book.toggle(_rec(1000, 2000))
    s = book.get("1|u/1|1000")
    s.state = RECORDING
    assert book.over(2000 + RUN_OVER - 1) == []
    assert [x.key for x in book.over(2000 + RUN_OVER)] == [s.key]


def test_a_programme_slept_through_is_missed_not_started_late():
    # The app was closed over the whole programme. Starting now would record
    # whatever is on the channel instead, which is not what was asked for.
    book = RecordingBook()
    book.toggle(_rec(1000, 2000))
    assert book.missed(1500) == []
    assert len(book.missed(2000 + RUN_OVER)) == 1


def test_overlapping_recordings_are_reported_not_refused():
    # Two at once is fine when the account has the connections for it; the
    # window warns, it does not decide.
    book = RecordingBook()
    book.toggle(_rec(1000, 2000, url="a"))
    clash = _rec(1900, 3000, url="b")
    assert [s.channel_url for s in book.clashes(clash)] == ["a"]
    # Truly apart: the next one starts early too, so the gap has to cover
    # this one's run-over and that one's lead-in.
    apart = _rec(2000 + RUN_OVER + LEAD_IN + 1, 4000, url="c")
    assert book.clashes(apart) == []
    # A finished one does not clash with anything.
    book.get("1|a|1000").state = DONE
    assert book.clashes(clash) == []


def test_finished_recordings_are_forgotten_eventually():
    book = RecordingBook()
    book.toggle(_rec(1000, 2000, state=DONE))
    book.toggle(_rec(1000, 2000, url="b", state=WAITING))
    assert book.prune(2000) is False
    assert book.prune(10**9) is True
    assert len(book) == 1  # the waiting one stays


def test_the_list_survives_being_saved_and_read_back():
    book = RecordingBook()
    book.toggle(_rec(1000, 2000, state=RECORDING, path="/x/y.ts"))
    again = RecordingBook(book.to_list())
    assert len(again) == 1
    s = again.get("1|u/1|1000")
    assert s.state == RECORDING and s.path == "/x/y.ts" and s.title == "Jornal"


def test_an_entry_from_another_version_is_skipped_not_fatal():
    book = RecordingBook([{"nonsense": 1}, {"playlist_id": 1, "channel_url": "u",
                                            "channel_name": "C", "title": "T",
                                            "start": 1, "stop": 2}])
    assert len(book) == 1


def test_a_title_becomes_a_usable_file_name():
    assert safe_name("Jornal: edição / das 20h", "x") == "Jornal edição das 20h"
    assert safe_name("", "Gravação") == "Gravação"
    assert safe_name("///", "Gravação") == "Gravação"
    # Accents are two bytes: the limit is counted in bytes, not characters.
    assert len(safe_name("ç" * 300, "x").encode()) <= 120


# ---- the worker ------------------------------------------------------


class _Chunks(http.server.BaseHTTPRequestHandler):
    """A channel that sends a little video and keeps the connection open."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        try:
            for _ in range(20):
                self.wfile.write(b"\x47" + b"\x00" * 187)
                self.wfile.flush()
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_worker_writes_the_stream_as_it_arrives(tmp_path):
    srv = _serve(_Chunks)
    try:
        path = tmp_path / "x.ts"
        done = []
        r = Recording(f"http://127.0.0.1:{srv.server_port}/live", path,
                      until=time.time() + 1.5, on_done=done.append)
        r.start()
        r._thread.join(timeout=10)
        assert not r.running
        assert done, "the recording did not report finishing"
        assert path.exists() and path.stat().st_size > 0
        # Byte for byte what was sent: no decoding, no re-encoding.
        assert path.read_bytes().startswith(b"\x47")
    finally:
        srv.shutdown()


def test_stopping_early_closes_the_file(tmp_path):
    srv = _serve(_Chunks)
    try:
        path = tmp_path / "x.ts"
        r = Recording(f"http://127.0.0.1:{srv.server_port}/live", path,
                      until=time.time() + 30)
        r.start()
        time.sleep(0.3)
        r.stop()
        r._thread.join(timeout=10)
        assert not r.running
        assert path.exists()
    finally:
        srv.shutdown()


def test_a_channel_that_answers_nothing_leaves_no_empty_file(tmp_path):
    class Empty(_Chunks):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

    srv = _serve(Empty)
    try:
        path = tmp_path / "x.ts"
        failed = []
        r = Recording(f"http://127.0.0.1:{srv.server_port}/live", path,
                      until=time.time() + 0.6,
                      on_fail=lambda rec, why: failed.append(why))
        r.start()
        r._thread.join(timeout=15)
        assert failed, "a channel that never answered was reported as recorded"
        assert not path.exists(), "an empty file was left behind"
    finally:
        srv.shutdown()
