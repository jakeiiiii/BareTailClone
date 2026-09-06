"""Byte-addressed random access into a log file of any size.

The file is never read into memory.  Every operation is expressed in terms of a
byte offset, and the cost of reading a window is proportional to the size of
that window rather than the size of the file.

Line numbers are the awkward part: knowing that a byte offset is line
1,234,567 requires having counted the newlines before it.  Storing an offset
per line is not viable -- a 20 GB log holds roughly 200 million lines, or
1.6 GB of offsets.  Instead a checkpoint is kept every ``CHECKPOINT_LINES``
lines, which for that same file is about 50,000 entries and a little over a
megabyte.  Any line number is then at most ``CHECKPOINT_LINES`` steps from a
known one.

Checkpoints are filled in by a background thread (see :mod:`.indexer`), so a
freshly opened file is usable -- scrollable and tailable -- long before it has
been counted.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from . import encoding as enc
from .fileopen import open_shared

__all__ = ["Line", "LineFile", "CHECKPOINT_LINES"]

#: Lines between index checkpoints.  Larger costs more forward scanning per
#: lookup, smaller costs more memory; 4096 keeps both comfortably small.
CHECKPOINT_LINES = 4096

#: Bytes read per indexing pass.
INDEX_CHUNK = 1 << 20

#: How far back to look for the start of the line containing an offset.  A
#: line longer than this is rendered from a mid-line boundary rather than
#: scanning arbitrarily far backwards, which is what keeps scrolling O(1) even
#: on a file with no newlines at all.
MAX_LINE_SCAN = 1 << 20

#: First read size when scanning backwards for a terminator, widened on miss.
#: Ordinary log lines are a few hundred bytes, so this almost always resolves
#: in one read; sizing every backward scan at MAX_LINE_SCAN instead cost about
#: 20 ms per repaint and dominated the entire view.
BACK_SCAN_START = 4096

#: Bytes compared to decide whether a file was replaced rather than appended.
SIGNATURE_BYTES = 512


@dataclass(frozen=True)
class Line:
    """One line of the file.

    ``offset`` is the absolute byte offset of the line's first byte, which is
    what the viewport stores and what jumping to a search hit uses.  ``number``
    is 0-based and may be ``None`` when the index has not reached this point
    yet.
    """

    offset: int
    text: str
    number: int | None = None


class LineFile:
    """Random access to a log file by byte offset.

    Safe to use from several threads: the file handle and the index are both
    guarded, so the indexer and tailer can extend the index while the UI reads
    a window for display.
    """

    def __init__(self, path: str, codec: enc.Codec | None = None,
                 default_codec: enc.Codec = enc.ANSI) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._fh = None
        self._forced_codec = codec
        self._default_codec = default_codec

        self.codec: enc.Codec = codec or default_codec
        self.content_start = 0
        self.signature = b""

        # Index state.  ``_checkpoints`` is (byte_offset, line_number) pairs in
        # ascending order, always starting with the first content byte.
        self._checkpoints: list[tuple[int, int]] = []
        self.indexed_to = 0
        self.indexed_lines = 0

        self._nl = b"\n"
        self._cr = b"\r"

        self.open()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the file and sniff its encoding and line ending."""
        with self._lock:
            self.close()
            # open_shared, not open(): the built-in withholds share-delete
            # on Windows and would block the writer from rotating its log.
            self._fh = open_shared(self.path)
            head = self._fh.read(max(SIGNATURE_BYTES, 4096))
            self.signature = head[:SIGNATURE_BYTES]

            if self._forced_codec is not None:
                self.codec = self._forced_codec
                has_bom = bool(self.codec.bom) and head.startswith(self.codec.bom)
                self.content_start = len(self.codec.bom) if has_bom else 0
            else:
                self.codec, self.content_start = enc.detect(head, self._default_codec)

            self._nl = self.codec.lf
            self._cr = self.codec.cr

            # A file using bare CR terminators has no LF at all; counting LFs
            # would report it as a single enormous line.
            body = head[self.content_start:]
            if self._cr in body and self._nl not in body:
                self._nl = self._cr

            self.reset_index()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    def __enter__(self) -> "LineFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Raw access
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Current size of the file in bytes, re-read on every access."""
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def read_bytes(self, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset``, clamped to the file."""
        if length <= 0:
            return b""
        with self._lock:
            if self._fh is None:
                return b""
            try:
                self._fh.seek(max(0, offset))
                return self._fh.read(length)
            except OSError:
                return b""

    def read_signature(self) -> bytes:
        """Re-read the opening bytes, for detecting file replacement."""
        return self.read_bytes(0, SIGNATURE_BYTES)

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def reset_index(self) -> None:
        """Discard the index, as after truncation or rotation."""
        with self._lock:
            self._checkpoints = [(self.content_start, 0)]
            self.indexed_to = self.content_start
            self.indexed_lines = 0

    @property
    def index_complete(self) -> bool:
        return self.indexed_to >= self.size

    @property
    def line_count(self) -> int:
        """Lines counted so far.

        Equals the true total once :attr:`index_complete`.  A trailing
        fragment with no terminator counts as a line, since it is displayed as
        one.
        """
        with self._lock:
            total = self.indexed_lines
            if self.indexed_to > self.content_start and not self._ends_with_newline():
                total += 1
            return total

    def _ends_with_newline(self) -> bool:
        end = self.indexed_to
        if end <= self.content_start:
            return True
        tail = self.read_bytes(end - len(self._nl), len(self._nl))
        return tail == self._nl

    def index_step(self, budget: int = INDEX_CHUNK) -> bool:
        """Index up to ``budget`` more bytes.

        Returns True while there is more to do.  Called repeatedly by the
        indexer thread so indexing stays interruptible and the file stays
        usable throughout.
        """
        with self._lock:
            start = self.indexed_to
            size = self.size
            if start >= size:
                return False

        chunk = self.read_bytes(start, budget)
        if not chunk:
            with self._lock:
                self.indexed_to = max(self.indexed_to, size)
            return False

        nl = self._nl
        unit = self.codec.unit
        with self._lock:
            line_no = self.indexed_lines
            pos = 0
            while True:
                found = chunk.find(nl, pos)
                if found < 0:
                    break
                # Keep two-byte terminators on their code-unit boundary; a
                # stray 0x0A byte inside a UTF-16 character is not a newline.
                if unit > 1 and (start + found) % unit != 0:
                    pos = found + 1
                    continue
                line_no += 1
                pos = found + len(nl)
                if line_no % CHECKPOINT_LINES == 0:
                    self._checkpoints.append((start + pos, line_no))

            self.indexed_lines = line_no
            # Stop at the last complete line so a terminator split across the
            # chunk edge is not missed; if none was found, take the whole
            # chunk to guarantee forward progress.
            self.indexed_to = start + (pos if pos else len(chunk))
            return self.indexed_to < self.size

    def _checkpoint_at_or_before(self, *, offset: int | None = None,
                                 line: int | None = None) -> tuple[int, int]:
        """Nearest checkpoint at or before a byte offset or line number."""
        with self._lock:
            points = self._checkpoints
            lo, hi = 0, len(points) - 1
            best = points[0]
            target = offset if offset is not None else line
            while lo <= hi:
                mid = (lo + hi) // 2
                value = points[mid][0] if offset is not None else points[mid][1]
                if value <= target:
                    best = points[mid]
                    lo = mid + 1
                else:
                    hi = mid - 1
            return best

    # ------------------------------------------------------------------
    # Line boundaries
    # ------------------------------------------------------------------

    def line_start_at_or_before(self, offset: int) -> int:
        """The start of the line containing ``offset``.

        Called on every repaint, so the read must be sized to the line rather
        than to the worst case: it starts at :data:`BACK_SCAN_START` and only
        widens when no terminator is found, which for ordinary log lines means
        a single small read.  Reading the full :data:`MAX_LINE_SCAN` window up
        front measured about 20 ms per repaint -- the dominant cost in the
        whole view -- against well under a millisecond this way.

        Beyond :data:`MAX_LINE_SCAN` the offset is returned as-is, so a file
        with no terminators at all still scrolls smoothly instead of scanning
        arbitrarily far backwards.
        """
        offset = max(self.content_start, min(offset, self.size))
        offset = self._align(offset)
        if offset <= self.content_start:
            return self.content_start

        window = BACK_SCAN_START
        while True:
            base = max(self.content_start, offset - window)
            buf = self.read_bytes(base, offset - base)
            found = self._rfind_aligned(buf, self._nl, base)
            if found >= 0:
                return base + found + len(self._nl)
            if base <= self.content_start:
                return self.content_start
            if window >= MAX_LINE_SCAN:
                return offset
            window = min(window * 8, MAX_LINE_SCAN)

    def _align(self, offset: int) -> int:
        unit = self.codec.unit
        return offset if unit == 1 else offset - (offset % unit)

    def _rfind_aligned(self, buf: bytes, pattern: bytes, base: int) -> int:
        """Reverse search honouring code-unit alignment."""
        unit = self.codec.unit
        pos = len(buf)
        while True:
            found = buf.rfind(pattern, 0, pos)
            if found < 0:
                return -1
            if unit == 1 or (base + found) % unit == 0:
                return found
            pos = found

    def _find_aligned(self, buf: bytes, pattern: bytes, base: int, start: int = 0) -> int:
        unit = self.codec.unit
        pos = start
        while True:
            found = buf.find(pattern, pos)
            if found < 0:
                return -1
            if unit == 1 or (base + found) % unit == 0:
                return found
            pos = found + 1

    # ------------------------------------------------------------------
    # Reading lines
    # ------------------------------------------------------------------

    def read_lines_at(self, offset: int, count: int) -> list[Line]:
        """Read ``count`` lines starting at the line containing ``offset``.

        This is what the viewport calls on every repaint, so it must not depend
        on the size of the file -- it seeks straight to the offset and reads
        forward only as far as the requested lines.
        """
        if count <= 0:
            return []

        size = self.size
        start = self.line_start_at_or_before(offset)
        if start >= size:
            return []

        lines: list[Line] = []
        pos = start
        pending = b""
        pending_at = start
        # Grow the read as needed rather than guessing a line length: most log
        # lines are short, but one long line must not cost a second round trip
        # per repaint.
        want = min(count * 256 + 4096, size - start)

        while len(lines) < count and pos < size:
            chunk = self.read_bytes(pos, max(want, 4096))
            if not chunk:
                break
            pos += len(chunk)
            buf = pending + chunk
            base = pending_at

            search = 0
            while len(lines) < count:
                found = self._find_aligned(buf, self._nl, base, search)
                if found < 0:
                    break
                raw = buf[search:found]
                lines.append(Line(base + search, self._decode_line(raw)))
                search = found + len(self._nl)

            pending = buf[search:]
            pending_at = base + search
            want = min(INDEX_CHUNK, max(4096, (count - len(lines)) * 256))

        # A final fragment with no terminator is a real line on screen.
        if len(lines) < count and pending:
            lines.append(Line(pending_at, self._decode_line(pending)))

        return lines

    def iter_lines(self, start: int = 0, stop: int | None = None):
        """Yield :class:`Line` objects from ``start`` onwards.

        Streams in chunks with a carry-over buffer, so a terminator split
        across a chunk edge is handled by construction rather than by
        overlapping the reads and hoping the overlap is wide enough.

        Used by search and filtering, which walk the whole file; the viewport
        uses :meth:`read_lines_at` instead because it wants a bounded window.
        """
        size = self.size if stop is None else min(stop, self.size)
        pos = self.line_start_at_or_before(start)
        pending = b""
        pending_at = pos

        while pos < size:
            chunk = self.read_bytes(pos, min(INDEX_CHUNK, size - pos))
            if not chunk:
                break
            pos += len(chunk)
            buf = pending + chunk
            base = pending_at

            search = 0
            while True:
                found = self._find_aligned(buf, self._nl, base, search)
                if found < 0:
                    break
                yield Line(base + search, self._decode_line(buf[search:found]))
                search = found + len(self._nl)

            pending = buf[search:]
            pending_at = base + search

        if pending:
            yield Line(pending_at, self._decode_line(pending))

    def _decode_line(self, raw: bytes) -> str:
        """Decode one line's bytes, handling any line-ending convention.

        Splitting on LF leaves a trailing CR on CRLF files, and leaves bare-CR
        content joined; handling both here keeps mixed-convention files -- which
        do occur when several writers append to one log -- displaying correctly.
        """
        cr = self._cr
        if raw.endswith(cr):
            raw = raw[: -len(cr)]
        text = self.codec.decode(raw)
        if "\r" in text:
            text = text.replace("\r", "")
        return text

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def line_number_at(self, offset: int) -> int:
        """0-based line number of the line containing ``offset``.

        Exact where the index reaches, and derived from the nearest checkpoint
        otherwise, so it costs at most one ``CHECKPOINT_LINES`` scan.
        """
        target = self.line_start_at_or_before(offset)
        base_offset, base_line = self._checkpoint_at_or_before(offset=target)
        if target <= base_offset:
            return base_line

        count = 0
        pos = base_offset
        while pos < target:
            chunk = self.read_bytes(pos, min(INDEX_CHUNK, target - pos))
            if not chunk:
                break
            search = 0
            while True:
                found = self._find_aligned(chunk, self._nl, pos, search)
                if found < 0:
                    break
                count += 1
                search = found + len(self._nl)
            pos += len(chunk)
        return base_line + count

    def offset_of_line(self, number: int) -> int:
        """Byte offset where 0-based line ``number`` starts."""
        if number <= 0:
            return self.content_start
        base_offset, base_line = self._checkpoint_at_or_before(line=number)
        return self.step_lines(base_offset, number - base_line)

    def step_lines(self, offset: int, delta: int) -> int:
        """Move ``delta`` lines from ``offset``, returning the new line start.

        Used for wheel, arrow and page scrolling, where ``delta`` is small.
        Clamps at both ends of the file.
        """
        start = self.line_start_at_or_before(offset)
        if delta == 0:
            return start
        if delta > 0:
            return self._step_forward(start, delta)
        return self._step_back(start, -delta)

    def _step_forward(self, start: int, count: int) -> int:
        size = self.size
        pos = start
        remaining = count
        # Size the first read to the distance actually being travelled; a
        # three-line wheel notch should not read a megabyte.
        window = max(BACK_SCAN_START, count * 256)
        while remaining > 0 and pos < size:
            chunk = self.read_bytes(pos, min(window, INDEX_CHUNK))
            if not chunk:
                break
            search = 0
            while remaining > 0:
                found = self._find_aligned(chunk, self._nl, pos, search)
                if found < 0:
                    break
                search = found + len(self._nl)
                remaining -= 1
            if remaining == 0:
                return pos + search
            pos += len(chunk)
            window = min(window * 4, INDEX_CHUNK)
        return min(pos, size)

    def _step_back(self, start: int, count: int) -> int:
        """Find the ``count``-th line start before the line start ``start``.

        Line starts are ``content_start`` plus every offset just past a
        terminator.  The terminator immediately before ``start`` is that
        line's own, and stepping over it would not move anywhere, so
        terminators at or beyond ``limit`` are skipped.

        Reads grow from :data:`BACK_SCAN_START` so that scrolling back a few
        lines costs a few kilobytes rather than a fixed megabyte.
        """
        nl_len = len(self._nl)
        limit = start - nl_len
        seen = 0
        pos = start
        window = max(BACK_SCAN_START, count * 256)

        while pos > self.content_start:
            base = max(self.content_start, pos - window)
            buf = self.read_bytes(base, pos - base)
            if not buf:
                break
            end = len(buf)
            while True:
                found = self._rfind_aligned(buf[:end], self._nl, base)
                if found < 0:
                    break
                end = found
                if base + found >= limit:
                    continue
                seen += 1
                if seen == count:
                    return base + found + nl_len
            pos = base
            window = min(window * 4, MAX_LINE_SCAN)
        return self.content_start

    def offset_of_last_page(self, count: int) -> int:
        """Offset that puts the final line at the bottom of a ``count``-line view.

        This is where follow-tail parks the viewport.
        """
        size = self.size
        if size <= self.content_start:
            return self.content_start
        start = self.line_start_at_or_before(size)
        # A file ending in a newline has an empty final position; back up so
        # the last real line is the one shown.
        if start >= size and start > self.content_start:
            start = self.step_lines(start, -1)
        if count > 1:
            return self.step_lines(start, -(count - 1))
        return start
