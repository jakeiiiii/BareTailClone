"""Tests for character set detection and line splitting."""

from __future__ import annotations

import codecs

import pytest

from baretail.core import encoding as enc


# ----------------------------------------------------------------------
# Byte-order marks
# ----------------------------------------------------------------------

@pytest.mark.parametrize("codec,bom", [
    (enc.UTF8, codecs.BOM_UTF8),
    (enc.UTF16LE, codecs.BOM_UTF16_LE),
    (enc.UTF16BE, codecs.BOM_UTF16_BE),
])
def test_bom_is_detected_and_its_length_reported(codec, bom):
    found, skip = enc.detect(bom + "hello".encode(codec.python_name))
    assert found is codec
    assert skip == len(bom)


def test_utf8_bom_is_not_shadowed_by_a_shorter_mark():
    # BOM_UTF8 is EF BB BF and BOM_UTF16_LE is FF FE -- no overlap, but the
    # ordering of the checks is load-bearing, so pin it.
    found, skip = enc.detect(codecs.BOM_UTF8 + b"text")
    assert found is enc.UTF8 and skip == 3


# ----------------------------------------------------------------------
# Detection without a mark
# ----------------------------------------------------------------------

def test_bomless_utf16le_detected_from_nul_pattern():
    found, skip = enc.detect("some plain log line\n".encode("utf-16-le"))
    assert found is enc.UTF16LE
    assert skip == 0


def test_bomless_utf16be_detected_from_nul_pattern():
    found, skip = enc.detect("some plain log line\n".encode("utf-16-be"))
    assert found is enc.UTF16BE
    assert skip == 0


def test_valid_utf8_without_a_bom():
    found, _ = enc.detect("café naïve ünïcode\n".encode("utf-8"))
    assert found is enc.UTF8


def test_ansi_bytes_are_not_claimed_as_utf8():
    # 0xE9 begins a three-byte UTF-8 sequence that never completes here.  This
    # is the exact case a naive "does a prefix decode?" check gets wrong.
    found, _ = enc.detect(b"caf\xe9 latte\n")
    assert found is enc.ANSI


def test_truncated_utf8_character_at_the_sample_edge_is_tolerated():
    # The sniffer only ever sees the head of the file, so the last character
    # may be cut in half; that must not demote the file to ANSI.
    sample = "aaaa".encode("utf-8") + "é".encode("utf-8")[:1]
    assert enc.detect(sample)[0] is enc.UTF8


def test_empty_input_falls_back_to_the_default():
    assert enc.detect(b"")[0] is enc.ANSI
    assert enc.detect(b"", default=enc.UTF8)[0] is enc.UTF8


def test_short_input_is_not_guessed_as_utf16():
    # Two bytes is not enough evidence of anything.
    assert enc.detect(b"a\x00")[0] is not enc.UTF16LE


def test_binary_content_falls_back_rather_than_raising():
    found, _ = enc.detect(bytes(range(128, 256)))
    assert found in enc.CODECS


# ----------------------------------------------------------------------
# Codec behaviour
# ----------------------------------------------------------------------

def test_newline_patterns_match_the_code_unit_width():
    assert enc.UTF8.lf == b"\n"
    assert enc.UTF16LE.lf == b"\n\x00"
    assert enc.UTF16BE.lf == b"\x00\n"
    assert enc.UTF16LE.unit == 2 and enc.UTF8.unit == 1


def test_decode_replaces_rather_than_raising():
    assert enc.UTF8.decode(b"ok \xff\xfe bad") .startswith("ok ")


def test_incremental_decoder_spans_a_chunk_boundary():
    # Reading a window out of the middle of a file routinely splits a
    # character; the decoder must carry the partial sequence across.
    data = "héllo wörld".encode("utf-8")
    split = data.index(b"\xc3") + 1
    decoder = enc.UTF8.incremental_decoder()
    out = decoder.decode(data[:split]) + decoder.decode(data[split:], True)
    assert out == "héllo wörld"


def test_align_rounds_down_to_a_code_unit():
    assert enc.UTF16LE.align(7) == 6
    assert enc.UTF16LE.align(8) == 8
    assert enc.UTF8.align(7) == 7


def test_by_label_round_trips_and_falls_back():
    for codec in enc.CODECS:
        assert enc.by_label(codec.label) is codec
    assert enc.by_label("nonsense") is enc.ANSI


# ----------------------------------------------------------------------
# Line splitting
# ----------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("a\nb\nc", ["a", "b", "c"]),
    ("a\r\nb\r\nc", ["a", "b", "c"]),
    ("a\rb\rc", ["a", "b", "c"]),
    ("a\r\nb\nc\rd", ["a", "b", "c", "d"]),
])
def test_split_lines_handles_every_convention(text, expected):
    assert enc.split_lines(text) == expected


def test_trailing_terminator_does_not_add_an_empty_line():
    assert enc.split_lines("a\nb\n") == ["a", "b"]
    assert enc.split_lines("a\r\nb\r\n") == ["a", "b"]


def test_interior_blank_lines_survive():
    assert enc.split_lines("a\n\n\nb\n") == ["a", "", "", "b"]


def test_empty_text_yields_no_lines():
    assert enc.split_lines("") == []


def test_split_lines_ignores_non_ascii_break_characters():
    # str.splitlines() would break on these; a log line containing a form feed
    # or U+2028 is still one line as far as the file is concerned.
    assert enc.split_lines("a\x0cb c") == ["a\x0cb c"]


def test_expand_tabs_uses_column_stops():
    assert enc.expand_tabs("a\tb", 4) == "a   b"
    assert enc.expand_tabs("ab\tc", 4) == "ab  c"
    assert enc.expand_tabs("a\tb", 0) == "a\tb"
