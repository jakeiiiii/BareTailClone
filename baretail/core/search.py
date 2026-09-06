"""Searching and filtering, the BareTailPro half of the feature set.

Two behaviours share one matcher:

* **Search** scans the file and streams every hit into a results table.
* **Filter tail** shows only the lines that match (or only those that do not),
  extending live as the file grows.

Both run on a worker thread and deliver results in batches, because a search
over a multi-gigabyte file produces results for a long time and the window has
to stay responsive throughout -- results appear as they are found rather than
after the scan completes.

Line iteration comes from :meth:`~.linefile.LineFile.iter_lines`, so the
chunk-boundary handling is the same code the viewport relies on rather than a
second implementation of it.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .linefile import Line, LineFile

__all__ = ["Matcher", "SearchHit", "SearchEngine", "FilterMode"]

#: Hits delivered per callback.  Batching keeps the UI queue from being handed
#: one message per match on a file with millions of them.
BATCH_SIZE = 200

#: Seconds between deliveries, so a slow trickle of matches still appears
#: promptly rather than waiting for a full batch.
BATCH_INTERVAL = 0.1


class FilterMode:
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class SearchHit:
    """One matching line."""

    offset: int
    text: str
    line_number: int | None = None
    groups: tuple[str, ...] = ()
    #: When the line was seen.  For a live filter this is when it arrived,
    #: which is what BareTailPro's timestamp column shows.
    timestamp: float = field(default_factory=time.time)


class Matcher:
    """A compiled literal or regular-expression matcher.

    An invalid pattern is not an exception: the user is typing, and the search
    bar shows the error next to the field while the previous results stay on
    screen.  So a broken pattern compiles to a matcher that matches nothing
    and reports why through :attr:`error`.
    """

    def __init__(self, pattern: str, is_regex: bool = False,
                 case_sensitive: bool = False, whole_word: bool = False) -> None:
        self.pattern = pattern
        self.is_regex = is_regex
        self.case_sensitive = case_sensitive
        self.whole_word = whole_word
        self.error: str | None = None
        self._compiled: re.Pattern | None = None

        if not pattern:
            return

        source = pattern if is_regex else re.escape(pattern)
        if whole_word:
            source = rf"\b(?:{source})\b"
        try:
            self._compiled = re.compile(source, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            self.error = str(exc)

    @property
    def valid(self) -> bool:
        return self.error is None

    @property
    def active(self) -> bool:
        """False for an empty or broken pattern, which matches nothing."""
        return self._compiled is not None

    def search(self, text: str) -> re.Match | None:
        return self._compiled.search(text) if self._compiled else None

    def matches(self, text: str) -> bool:
        return self.search(text) is not None

    def groups_for(self, text: str) -> tuple[str, ...]:
        """Capture groups of the first match, blanks for groups that did not
        participate, so the results table has a value for every column."""
        found = self.search(text)
        if found is None:
            return ()
        return tuple(g if g is not None else "" for g in found.groups())

    @property
    def group_count(self) -> int:
        return self._compiled.groups if self._compiled else 0

    def __repr__(self) -> str:
        kind = "regex" if self.is_regex else "literal"
        return f"<Matcher {kind} {self.pattern!r}{' INVALID' if self.error else ''}>"


class SearchEngine:
    """Runs a scan over a file on a background thread.

    Only one scan is active at a time: starting another cancels the first,
    which is what makes incremental search practical -- each keystroke
    supersedes the search still running from the previous one.
    """

    def __init__(self, linefile: LineFile,
                 on_hits: Callable[[list[SearchHit]], None] | None = None,
                 on_done: Callable[[int, bool], None] | None = None,
                 on_progress: Callable[[int, int], None] | None = None) -> None:
        self._file = linefile
        self._on_hits = on_hits
        self._on_done = on_done
        self._on_progress = on_progress

        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self, matcher: Matcher, start_offset: int = 0,
              limit: int | None = None, with_line_numbers: bool = True,
              invert: bool = False) -> None:
        """Begin a scan, cancelling any scan already running.

        ``invert`` yields the lines that do *not* match, which is what
        exclude-mode filtering needs; it shares this scan rather than having a
        second one of its own.
        """
        self.cancel()
        if not matcher.active:
            self._finish(0, True)
            return

        self._cancel = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(matcher, start_offset, limit, with_line_numbers,
                                    invert, self._cancel),
            name="search", daemon=True)
        self._thread.start()

    def cancel(self, timeout: float = 2.0) -> None:
        self._cancel.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------

    def _run(self, matcher: Matcher, start_offset: int, limit: int | None,
             with_line_numbers: bool, invert: bool, cancel: threading.Event) -> None:
        batch: list[SearchHit] = []
        total = 0
        last_flush = time.monotonic()
        completed = False

        # Numbering from the nearest checkpoint and counting forward is far
        # cheaper than asking for each hit's line number independently, which
        # would rescan from a checkpoint every time.
        line_no = self._file.line_number_at(start_offset) if with_line_numbers else 0

        try:
            for line in self._file.iter_lines(start_offset):
                if cancel.is_set():
                    break

                if matcher.matches(line.text) != invert:
                    batch.append(SearchHit(
                        offset=line.offset,
                        text=line.text,
                        line_number=line_no if with_line_numbers else None,
                        groups=() if invert else matcher.groups_for(line.text),
                    ))
                    total += 1

                line_no += 1

                now = time.monotonic()
                if len(batch) >= BATCH_SIZE or (batch and now - last_flush >= BATCH_INTERVAL):
                    self._deliver(batch)
                    batch = []
                    last_flush = now
                    if self._on_progress is not None:
                        self._on_progress(line.offset, self._file.size)

                if limit is not None and total >= limit:
                    break
            else:
                completed = True
        except OSError:
            # The file can be rotated out from under a long scan.
            pass

        if batch and not cancel.is_set():
            self._deliver(batch)
        if not cancel.is_set():
            self._finish(total, completed)

    def _deliver(self, batch: list[SearchHit]) -> None:
        if self._on_hits is not None and batch:
            try:
                self._on_hits(batch)
            except Exception:
                pass

    def _finish(self, total: int, completed: bool) -> None:
        if self._on_done is not None:
            try:
                self._on_done(total, completed)
            except Exception:
                pass


def find_next(linefile: LineFile, matcher: Matcher, from_offset: int,
              backwards: bool = False) -> Line | None:
    """Find the single next match after ``from_offset``.

    Backs F3 / Find Next, where the user wants one result now rather than a
    table of all of them.  Searching backwards has no streaming equivalent, so
    it scans forward from the start of the file and keeps the last match before
    the cursor -- acceptable because it is bounded by the current position
    rather than by the file, and only runs on an explicit keystroke.
    """
    if not matcher.active:
        return None

    if not backwards:
        for line in linefile.iter_lines(from_offset):
            if line.offset > from_offset and matcher.matches(line.text):
                return line
        return None

    best: Line | None = None
    for line in linefile.iter_lines(linefile.content_start, stop=from_offset):
        if line.offset < from_offset and matcher.matches(line.text):
            best = line
    return best


def iter_filtered(linefile: LineFile, matcher: Matcher, mode: str = FilterMode.INCLUDE,
                  start: int = 0) -> Iterator[Line]:
    """Yield the lines a filter keeps.

    ``INCLUDE`` keeps matching lines, ``EXCLUDE`` keeps the rest -- the two
    halves of BareTailPro's filter-tail mode.
    """
    include = mode == FilterMode.INCLUDE
    for line in linefile.iter_lines(start):
        if matcher.matches(line.text) == include:
            yield line
