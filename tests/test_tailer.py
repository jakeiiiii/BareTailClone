"""Tests for change detection and background indexing.

The tailer is driven synchronously through ``poll()`` rather than by starting
its thread and sleeping, so these are deterministic.
"""

from __future__ import annotations

import os

import pytest

from baretail.core.indexer import Indexer
from baretail.core.linefile import LineFile
from baretail.core.tailer import ChangeKind, Tailer


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "app.log"
    path.write_bytes(b"one\ntwo\n")
    lf = LineFile(str(path))
    yield str(path), lf
    lf.close()


def record(lf, **kwargs):
    """A tailer that appends every change it sees to a list."""
    events = []
    tailer = Tailer(lf, on_change=lambda kind, size: events.append((kind, size)), **kwargs)
    return tailer, events


# ----------------------------------------------------------------------
# Growth
# ----------------------------------------------------------------------

def test_quiet_file_reports_nothing(log):
    path, lf = log
    tailer, events = record(lf)
    assert tailer.poll() is None
    assert events == []


def test_append_is_reported_as_growth(log):
    path, lf = log
    tailer, events = record(lf)
    with open(path, "ab") as fh:
        fh.write(b"three\n")
    assert tailer.poll() == ChangeKind.GREW
    assert events[0][0] == ChangeKind.GREW
    assert events[0][1] == os.path.getsize(path)


def test_repeated_appends_each_report_once(log):
    path, lf = log
    tailer, events = record(lf)
    for text in (b"a\n", b"b\n", b"c\n"):
        with open(path, "ab") as fh:
            fh.write(text)
        tailer.poll()
    assert [kind for kind, _ in events] == [ChangeKind.GREW] * 3
    # And a poll with nothing new stays silent.
    assert tailer.poll() is None


def test_new_content_is_readable_after_growth(log):
    path, lf = log
    tailer, _ = record(lf)
    with open(path, "ab") as fh:
        fh.write(b"three\nfour\n")
    tailer.poll()
    last = lf.read_lines_at(lf.offset_of_last_page(1), 1)
    assert last[0].text == "four"


# ----------------------------------------------------------------------
# Truncation
# ----------------------------------------------------------------------

def test_truncation_in_place_is_detected(log):
    path, lf = log
    while lf.index_step():
        pass
    assert lf.line_count == 2

    tailer, events = record(lf)
    with open(path, "r+b") as fh:
        fh.truncate(0)
    assert tailer.poll() == ChangeKind.TRUNCATED
    # Offsets from the old content are meaningless now, so the index must go.
    assert lf.indexed_lines == 0
    assert lf.line_count == 0


def test_writing_after_truncation_reads_the_new_content(log):
    path, lf = log
    tailer, _ = record(lf)
    with open(path, "wb") as fh:
        fh.write(b"fresh start\n")
    tailer.poll()
    assert lf.read_lines_at(0, 5)[0].text == "fresh start"


def test_shrinking_to_a_smaller_nonzero_size_is_truncation(log):
    path, lf = log
    tailer, events = record(lf)
    with open(path, "r+b") as fh:
        fh.truncate(4)
    assert tailer.poll() == ChangeKind.TRUNCATED


# ----------------------------------------------------------------------
# Rotation
# ----------------------------------------------------------------------

def test_rotation_reopens_the_new_file(tmp_path):
    path = tmp_path / "rot.log"
    path.write_bytes(b"original one\noriginal two\n")
    lf = LineFile(str(path))
    tailer, events = record(lf)

    # Rename away and put a different file in place, which is what a log
    # rotator does.  The open handle still refers to the old file.
    os.replace(str(path), str(tmp_path / "rot.log.1"))
    (tmp_path / "rot.log").write_bytes(b"rotated content\n")

    assert tailer.poll() == ChangeKind.ROTATED
    # The critical assertion: we are now reading the *new* file, not the
    # renamed-away one our handle was opened on.
    assert lf.read_lines_at(0, 5)[0].text == "rotated content"
    lf.close()


def test_rotation_resets_the_line_count(tmp_path):
    path = tmp_path / "rot.log"
    path.write_bytes(b"a\nb\nc\nd\n")
    lf = LineFile(str(path))
    while lf.index_step():
        pass
    assert lf.line_count == 4

    tailer, _ = record(lf)
    os.replace(str(path), str(tmp_path / "rot.log.1"))
    (tmp_path / "rot.log").write_bytes(b"only one\n")
    tailer.poll()
    while lf.index_step():
        pass
    assert lf.line_count == 1
    lf.close()


def test_file_disappearing_and_returning(tmp_path):
    path = tmp_path / "gone.log"
    path.write_bytes(b"before\n")
    lf = LineFile(str(path))
    tailer, events = record(lf)

    os.remove(str(path))
    assert tailer.poll() == ChangeKind.VANISHED
    # Still gone: reported once, not on every poll.
    assert tailer.poll() is None

    path.write_bytes(b"after\n")
    assert tailer.poll() == ChangeKind.RESTORED
    assert lf.read_lines_at(0, 5)[0].text == "after"
    lf.close()


def test_a_failing_callback_does_not_stop_the_watch(log):
    path, lf = log

    def explode(kind, size):
        raise RuntimeError("callback bug")

    tailer = Tailer(lf, on_change=explode)
    with open(path, "ab") as fh:
        fh.write(b"three\n")
    # The change is still detected and reported despite the handler raising.
    assert tailer.poll() == ChangeKind.GREW


# ----------------------------------------------------------------------
# Indexer thread
# ----------------------------------------------------------------------

def test_indexer_counts_the_whole_file(tmp_path):
    path = tmp_path / "count.log"
    path.write_bytes(b"".join(b"line %d\n" % i for i in range(20000)))
    lf = LineFile(str(path))
    indexer = Indexer(lf, chunk=8192)
    indexer.start()
    assert indexer.wait_complete(10.0), "indexing did not finish"
    indexer.stop()
    assert lf.line_count == 20000
    assert lf.index_complete
    lf.close()


def test_indexer_reports_progress_and_completion(tmp_path):
    path = tmp_path / "progress.log"
    path.write_bytes(b"".join(b"line %d\n" % i for i in range(50000)))
    lf = LineFile(str(path))

    seen = []
    indexer = Indexer(lf, on_progress=lambda *args: seen.append(args), chunk=4096)
    indexer.start()
    assert indexer.wait_complete(10.0)
    indexer.stop()

    assert seen, "no progress was reported"
    # The final report is forced through the throttle, so the true count is
    # never left unreported.
    assert seen[-1][3] is True
    assert seen[-1][2] == 50000
    lf.close()


def test_indexer_picks_up_growth_after_notify(tmp_path):
    path = tmp_path / "grow.log"
    path.write_bytes(b"one\ntwo\n")
    lf = LineFile(str(path))
    indexer = Indexer(lf)
    indexer.start()
    assert indexer.wait_complete(5.0)
    assert lf.line_count == 2

    with open(path, "ab") as fh:
        fh.write(b"".join(b"more %d\n" % i for i in range(1000)))
    indexer.notify()
    assert indexer.wait_complete(5.0)
    indexer.stop()
    assert lf.line_count == 1002
    lf.close()


def test_indexer_stops_cleanly(tmp_path):
    path = tmp_path / "stop.log"
    path.write_bytes(b"x\n" * 10000)
    lf = LineFile(str(path))
    indexer = Indexer(lf)
    indexer.start()
    indexer.stop()
    assert not indexer.running
    lf.close()
