"""Background line-counting for an open file.

Counting the lines of a large file takes time proportional to its size, but
none of the viewer's behaviour may wait for it: opening a 20 GB log has to
put content on screen and start following immediately.

This thread therefore builds :class:`~.linefile.LineFile`'s checkpoints
gradually in the background.  Everything works throughout -- scrolling, tailing
and searching are all byte-addressed -- and the parts that genuinely need line
numbers simply become exact as the count catches up.

No widget is ever touched from here.  Progress is reported through a callback
that the UI layer marshals onto the main thread.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .linefile import INDEX_CHUNK, LineFile

__all__ = ["Indexer"]

#: Minimum gap between progress callbacks.  Indexing a large file completes
#: thousands of chunks; reporting each one would swamp the UI queue with
#: updates nobody can read.
PROGRESS_INTERVAL = 0.1

#: Pause between polls once the file is fully indexed, as a backstop for growth
#: that arrives without an explicit notify().
IDLE_POLL = 0.5


class Indexer:
    """Builds a file's line index on a daemon thread.

    ``on_progress`` is called as ``(indexed_bytes, total_bytes, line_count,
    complete)`` from the worker thread, throttled to
    :data:`PROGRESS_INTERVAL`, plus once unthrottled whenever indexing
    completes so the final count is never missed.
    """

    def __init__(self, linefile: LineFile,
                 on_progress: Callable[[int, int, int, bool], None] | None = None,
                 chunk: int = INDEX_CHUNK) -> None:
        self._file = linefile
        self._on_progress = on_progress
        self._chunk = chunk

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._complete = threading.Event()
        self._last_report = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"indexer:{self._file.path}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the thread to finish and wait briefly for it."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def notify(self) -> None:
        """Tell the indexer the file has grown or been replaced.

        Cheaper and more responsive than waiting for the idle poll; the tailer
        calls this the moment it sees a size change.
        """
        self._complete.clear()
        self._wake.set()

    def wait_complete(self, timeout: float | None = None) -> bool:
        """Block until the index covers the whole file. Intended for tests."""
        return self._complete.wait(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            worked = False
            try:
                # Yield between chunks rather than holding the file's lock for
                # a whole pass, so a repaint never waits on indexing.
                while not self._stop.is_set() and self._file.index_step(self._chunk):
                    worked = True
                    self._report(False)
            except OSError:
                # The file may vanish mid-scan (rotation).  The tailer owns
                # recovery; here it just means there is nothing to index.
                pass

            if self._stop.is_set():
                break

            if worked or not self._complete.is_set():
                self._complete.set()
                self._report(True, force=True)

            # Sleep until told the file grew, with a poll as a safety net.
            self._wake.wait(IDLE_POLL)
            self._wake.clear()

    def _report(self, complete: bool, force: bool = False) -> None:
        if self._on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_report < PROGRESS_INTERVAL:
            return
        self._last_report = now
        try:
            self._on_progress(
                self._file.indexed_to, self._file.size,
                self._file.line_count, complete)
        except Exception:
            # A failing UI callback must not kill indexing.
            pass
