"""Tests for byte-addressed file access.

These run headless -- no tkinter -- and cover the cases that a log viewer
actually meets: files that do not end in a newline, files still being written,
every line-ending convention, and offsets that land beyond an index checkpoint.
"""

from __future__ import annotations

import pytest

from baretail.core import encoding as enc
from baretail.core.linefile import CHECKPOINT_LINES, LineFile


def write(tmp_path, name, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def fully_indexed(lf: LineFile) -> LineFile:
    while lf.index_step():
        pass
    return lf


# ----------------------------------------------------------------------
# Line endings and encodings
# ----------------------------------------------------------------------

@pytest.mark.parametrize("terminator", [b"\n", b"\r\n", b"\r"])
def test_all_three_line_endings(tmp_path, terminator):
    body = terminator.join([b"alpha", b"beta", b"gamma"]) + terminator
    with LineFile(write(tmp_path, "t.log", body)) as lf:
        lines = lf.read_lines_at(0, 10)
        assert [line.text for line in lines] == ["alpha", "beta", "gamma"]
        assert fully_indexed(lf).line_count == 3


def test_mixed_line_endings_in_one_file(tmp_path):
    # Several writers appending to one log is a real situation, and BareTail
    # displays such a file as the individual lines it is made of.
    body = b"alpha\r\nbeta\ngamma\r\n"
    with LineFile(write(tmp_path, "mixed.log", body)) as lf:
        assert [ln.text for ln in lf.read_lines_at(0, 10)] == ["alpha", "beta", "gamma"]


def test_utf8_bom_is_not_shown_as_text(tmp_path):
    body = b"\xef\xbb\xbfcaf\xc3\xa9\nna\xc3\xafve\n"
    with LineFile(write(tmp_path, "u8.log", body)) as lf:
        assert lf.codec is enc.UTF8
        assert lf.content_start == 3
        assert [ln.text for ln in lf.read_lines_at(0, 5)] == ["café", "naïve"]


def test_utf16le_offsets_stay_on_code_unit_boundaries(tmp_path):
    text = "alpha\nbeta\ngamma\n"
    body = b"\xff\xfe" + text.encode("utf-16-le")
    with LineFile(write(tmp_path, "u16.log", body)) as lf:
        assert lf.codec is enc.UTF16LE
        assert [ln.text for ln in lf.read_lines_at(0, 5)] == ["alpha", "beta", "gamma"]
        assert fully_indexed(lf).line_count == 3
        # Every line must begin on an even byte, BOM included.
        assert all(ln.offset % 2 == 0 for ln in lf.read_lines_at(0, 5))


def test_utf16_ascii_byte_is_not_mistaken_for_a_newline(tmp_path):
    # U+0A41 encodes little-endian as 41 0A: the 0x0A is the *high* byte of a
    # character, not a terminator.  Splitting on it would corrupt the line.
    text = "aੁb\nsecond\n"
    body = b"\xff\xfe" + text.encode("utf-16-le")
    with LineFile(write(tmp_path, "u16nl.log", body)) as lf:
        assert [ln.text for ln in lf.read_lines_at(0, 5)] == ["aੁb", "second"]


def test_ansi_fallback_for_non_utf8_bytes(tmp_path):
    with LineFile(write(tmp_path, "ansi.log", b"caf\xe9\n")) as lf:
        assert lf.codec is enc.ANSI
        assert lf.read_lines_at(0, 1)[0].text == "café"


def test_malformed_bytes_do_not_raise(tmp_path):
    body = b"good line\n\xff\xfe\xff bad \x80\x81\ntail\n"
    with LineFile(write(tmp_path, "bad.log", body), codec=enc.UTF8) as lf:
        lines = lf.read_lines_at(0, 5)
        assert len(lines) == 3
        assert lines[0].text == "good line"


# ----------------------------------------------------------------------
# Edge-shaped files
# ----------------------------------------------------------------------

def test_empty_file(tmp_path):
    with LineFile(write(tmp_path, "empty.log", b"")) as lf:
        assert lf.read_lines_at(0, 10) == []
        assert lf.line_count == 0
        assert lf.offset_of_last_page(10) == 0
        assert lf.index_step() is False


def test_trailing_fragment_without_newline_is_a_line(tmp_path):
    # The last line of a file being actively written has no terminator yet;
    # it still has to appear on screen.
    with LineFile(write(tmp_path, "partial.log", b"first\nsecond")) as lf:
        assert [ln.text for ln in lf.read_lines_at(0, 5)] == ["first", "second"]
        assert fully_indexed(lf).line_count == 2


def test_single_line_longer_than_the_read_window(tmp_path):
    body = b"x" * (300 * 1024) + b"\nshort\n"
    with LineFile(write(tmp_path, "long.log", body)) as lf:
        lines = lf.read_lines_at(0, 2)
        assert len(lines[0].text) == 300 * 1024
        assert lines[1].text == "short"


def test_file_with_no_newline_at_all(tmp_path):
    with LineFile(write(tmp_path, "one.log", b"solitary")) as lf:
        assert [ln.text for ln in lf.read_lines_at(0, 5)] == ["solitary"]
        assert lf.line_start_at_or_before(4) == 0


def test_blank_lines_are_preserved(tmp_path):
    with LineFile(write(tmp_path, "blank.log", b"a\n\n\nb\n")) as lf:
        assert [ln.text for ln in lf.read_lines_at(0, 10)] == ["a", "", "", "b"]


# ----------------------------------------------------------------------
# Navigation, and the index behind it
# ----------------------------------------------------------------------

@pytest.fixture
def big(tmp_path):
    """A file spanning several index checkpoints."""
    count = CHECKPOINT_LINES * 3 + 17
    body = b"".join(b"line %06d padding\n" % i for i in range(count))
    return write(tmp_path, "big.log", body), count


def test_line_number_and_offset_round_trip_across_checkpoints(big):
    path, count = big
    with LineFile(path) as lf:
        fully_indexed(lf)
        assert lf.line_count == count
        # Deliberately probe on, either side of, and far from checkpoints.
        probes = [0, 1, CHECKPOINT_LINES - 1, CHECKPOINT_LINES,
                  CHECKPOINT_LINES + 1, CHECKPOINT_LINES * 2,
                  CHECKPOINT_LINES * 3 + 16, count - 1]
        for n in probes:
            offset = lf.offset_of_line(n)
            assert lf.line_number_at(offset) == n, f"line {n}"
            assert lf.read_lines_at(offset, 1)[0].text == "line %06d padding" % n


def test_navigation_works_before_indexing_finishes(big):
    path, count = big
    with LineFile(path) as lf:
        # No index_step() at all: a freshly opened file must already scroll.
        assert lf.read_lines_at(0, 1)[0].text == "line 000000 padding"
        last = lf.offset_of_last_page(1)
        assert lf.read_lines_at(last, 1)[0].text == "line %06d padding" % (count - 1)


def test_step_lines_forward_and_back(big):
    path, count = big
    with LineFile(path) as lf:
        start = lf.offset_of_line(1000)
        assert lf.line_number_at(lf.step_lines(start, 50)) == 1050
        assert lf.line_number_at(lf.step_lines(start, -50)) == 950
        assert lf.step_lines(start, 0) == start


def test_step_lines_clamps_at_both_ends(big):
    path, count = big
    with LineFile(path) as lf:
        assert lf.step_lines(0, -100) == 0
        end = lf.step_lines(lf.size, 100)
        assert end <= lf.size


def test_offset_of_last_page_shows_the_final_lines(big):
    path, count = big
    with LineFile(path) as lf:
        top = lf.offset_of_last_page(25)
        lines = lf.read_lines_at(top, 25)
        assert len(lines) == 25
        assert lines[-1].text == "line %06d padding" % (count - 1)


def test_line_start_snaps_to_the_containing_line(big):
    path, _ = big
    with LineFile(path) as lf:
        start = lf.offset_of_line(500)
        # Any offset inside the line resolves back to that line's first byte.
        for probe in (start, start + 1, start + 7):
            assert lf.line_start_at_or_before(probe) == start


def test_reading_past_the_end_returns_what_exists(big):
    path, count = big
    with LineFile(path) as lf:
        near_end = lf.offset_of_line(count - 3)
        assert len(lf.read_lines_at(near_end, 100)) == 3
        assert lf.read_lines_at(lf.size + 5000, 10) == []


def test_index_is_incremental_and_resumable(big):
    path, count = big
    with LineFile(path) as lf:
        steps = 0
        while lf.index_step(budget=4096):
            steps += 1
            assert steps < 10000, "indexing failed to make progress"
        assert lf.index_complete
        assert lf.line_count == count


def test_index_reset_after_truncation(tmp_path):
    path = write(tmp_path, "rot.log", b"one\ntwo\nthree\n")
    with LineFile(path) as lf:
        fully_indexed(lf)
        assert lf.line_count == 3
        with open(path, "wb") as fh:
            fh.write(b"fresh\n")
        lf.reset_index()
        fully_indexed(lf)
        assert lf.line_count == 1
        assert lf.read_lines_at(0, 5)[0].text == "fresh"


def test_appended_data_is_picked_up_without_reopening(tmp_path):
    path = write(tmp_path, "grow.log", b"one\ntwo\n")
    with LineFile(path) as lf:
        fully_indexed(lf)
        assert lf.line_count == 2
        with open(path, "ab") as fh:
            fh.write(b"three\nfour\n")
        fully_indexed(lf)
        assert lf.line_count == 4
        assert lf.read_lines_at(lf.offset_of_last_page(1), 1)[0].text == "four"
