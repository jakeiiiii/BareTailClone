"""Watching a log file for changes.

Detection is by polling ``os.stat`` rather than by ``ReadDirectoryChangesW``.
That is a deliberate choice: BareTail explicitly supports files on network
shares, where change notifications are unreliable or unavailable, and a stat
every quarter second costs nothing next to the reading the viewer does anyway.

Three things can happen to a log file, and only one of them is growth:

* **Appended** -- the ordinary case.  The index extends and, if following, the
  view re-anchors to the end.
* **Truncated** -- an application reopened its log with ``w`` mode.  Offsets
  into the old content are now meaningless, so the index is discarded.
* **Rotated** -- the file was renamed away and a new one put in its place.  The
  open handle still refers to the *old* file, invisibly, so it must be
  reopened or the viewer silently keeps showing a file no longer on disk.

Callbacks fire on the polling thread; the UI layer marshals them onto the main
thread.
"""

from __future__ import annotations

import os
import threading
from typing import Callable

from .linefile import LineFile

__all__ = ["Tailer", "ChangeKind"]

#: Default seconds between stat calls.
DEFAULT_INTERVAL = 0.25


class ChangeKind:
    GREW = "grew"
    TRUNCATED = "truncated"
    ROTATED = "rotated"
    VANISHED = "vanished"
    RESTORED = "restored"


class _Identity:
    """The stat fields that distinguish one file from another at a path."""

    __slots__ = ("ino", "dev", "ctime", "size")

    def __init__(self, st: os.stat_result | None) -> None:
        self.ino = getattr(st, "st_ino", 0) if st else 0
        self.dev = getattr(st, "st_dev", 0) if st else 0
        self.ctime = getattr(st, "st_ctime", 0) if st else 0
        self.size = st.st_size if st else -1

    def is_same_file(self, other: "_Identity") -> bool:
        """Whether two observations refer to the same underlying file.

        The inode/device pair is authoritative where the filesystem provides
        it.  Some network filesystems report zeros, so creation time is used
        as a fallback rather than assuming identity.
        """
        if self.ino and other.ino:
            return self.ino == other.ino and self.dev == other.dev
        return self.ctime == other.ctime


class Tailer:
    """Polls a file and reports how it changed.

    ``on_change(kind, size)`` is called for each transition.  The
    :class:`LineFile` is already reopened and its index reset before a
    ``ROTATED`` or ``TRUNCATED`` callback arrives, so the handler only has to
    update the display.
    """

    def __init__(self, linefile: LineFile,
                 on_change: Callable[[str, int], None] | None = None,
                 interval: float = DEFAULT_INTERVAL) -> None:
        self._file = linefile
        self._on_change = on_change
        self._interval = interval

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._identity = _Identity(self._stat())
        self._missing = False

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"tailer:{self._file.path}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def set_interval(self, seconds: float) -> None:
        self._interval = max(0.02, seconds)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------

    def _stat(self) -> os.stat_result | None:
        try:
            return os.stat(self._file.path)
        except OSError:
            return None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll()
            except OSError:
                # A transient failure -- a share dropping out, a file locked
                # mid-rotation -- is not fatal; the next poll tries again.
                pass

    def poll(self) -> str | None:
        """Check the file once and report any change.

        Public and synchronous so tests can drive it deterministically rather
        than sleeping and hoping.
        """
        st = self._stat()
        current = _Identity(st)

        if st is None:
            # Rotation often has a window where the path does not exist.
            if not self._missing:
                self._missing = True
                return self._emit(ChangeKind.VANISHED, 0)
            return None

        if self._missing:
            self._missing = False
            self._identity = current
            self._reopen()
            return self._emit(ChangeKind.RESTORED, current.size)

        if not current.is_same_file(self._identity):
            self._identity = current
            self._reopen()
            return self._emit(ChangeKind.ROTATED, current.size)

        previous_size = self._identity.size
        self._identity = current

        if current.size < previous_size:
            # Same file, less content: it was truncated in place.  Every
            # cached offset now points somewhere else.
            self._file.reset_index()
            return self._emit(ChangeKind.TRUNCATED, current.size)

        if current.size > previous_size:
            return self._emit(ChangeKind.GREW, current.size)

        return None

    def _reopen(self) -> None:
        """Rebind to the file now at the path and drop stale state."""
        try:
            self._file.open()
        except OSError:
            self._file.reset_index()

    def _emit(self, kind: str, size: int) -> str:
        if self._on_change is not None:
            try:
                self._on_change(kind, size)
            except Exception:
                # A failing UI callback must not stop the watch.
                pass
        return kind
