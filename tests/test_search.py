"""Tests for matching, searching and filtering."""

from __future__ import annotations

import threading

import pytest

from baretail.core.highlight import DEFAULT_RULES, HighlightRule, RuleSet
from baretail.core.linefile import INDEX_CHUNK, LineFile
from baretail.core.search import (
    FilterMode, Matcher, SearchEngine, find_next, iter_filtered,
)


@pytest.fixture
def sample(tmp_path):
    body = (
        b"2024-01-01 INFO  starting up\n"
        b"2024-01-01 ERROR disk full on /dev/sda1\n"
        b"2024-01-01 WARN  retrying in 5s\n"
        b"2024-01-01 INFO  connected to peer 42\n"
        b"2024-01-01 ERROR timeout after 30000ms\n"
        b"2024-01-01 DEBUG cache warm\n"
    )
    path = tmp_path / "s.log"
    path.write_bytes(body)
    lf = LineFile(str(path))
    yield lf
    lf.close()


# ----------------------------------------------------------------------
# Matcher
# ----------------------------------------------------------------------

def test_literal_match_is_case_insensitive_by_default():
    assert Matcher("error").matches("An ERROR occurred")
    assert not Matcher("error", case_sensitive=True).matches("An ERROR occurred")


def test_literal_pattern_does_not_act_as_a_regex():
    # A user searching for "a.b" wants that text, not "a<any>b".
    assert Matcher("a.b").matches("xxa.byy")
    assert not Matcher("a.b").matches("xxaXbyy")


def test_regex_mode_enables_metacharacters():
    assert Matcher(r"\d{5}", is_regex=True).matches("code 12345 here")
    assert not Matcher(r"\d{5}", is_regex=True).matches("code 123 here")


def test_whole_word_option():
    assert not Matcher("cat", whole_word=True).matches("concatenate")
    assert Matcher("cat", whole_word=True).matches("the cat sat")


def test_invalid_regex_is_reported_not_raised():
    # The search bar shows this while the user is mid-edit; raising would
    # tear down the search as soon as they typed an opening bracket.
    matcher = Matcher("[unclosed", is_regex=True)
    assert not matcher.valid
    assert matcher.error
    assert not matcher.active
    assert matcher.matches("anything") is False


def test_empty_pattern_matches_nothing():
    matcher = Matcher("")
    assert not matcher.active
    assert matcher.valid
    assert not matcher.matches("text")


def test_capture_groups_are_extracted():
    matcher = Matcher(r"(\w+) after (\d+)ms", is_regex=True)
    assert matcher.group_count == 2
    assert matcher.groups_for("timeout after 30000ms") == ("timeout", "30000")


def test_non_participating_groups_become_blanks():
    # The results table needs a value per column even when an alternative
    # branch did not match.
    matcher = Matcher(r"(a)|(b)", is_regex=True)
    assert matcher.groups_for("b") == ("", "b")


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------

def run_search(lf, matcher, **kwargs):
    """Run a search to completion and return its hits."""
    hits = []
    done = threading.Event()
    result = {}

    def finished(total, completed):
        result["total"] = total
        result["completed"] = completed
        done.set()

    engine = SearchEngine(lf, on_hits=hits.extend, on_done=finished)
    engine.start(matcher, **kwargs)
    assert done.wait(15.0), "search did not finish"
    engine.cancel()
    return hits, result


def test_search_finds_every_match(sample):
    hits, result = run_search(sample, Matcher("ERROR"))
    assert [h.text.split()[1] for h in hits] == ["ERROR", "ERROR"]
    assert result["total"] == 2
    assert result["completed"] is True


def test_hits_carry_offset_and_line_number(sample):
    hits, _ = run_search(sample, Matcher("ERROR"))
    assert [h.line_number for h in hits] == [1, 4]
    # The offset must land on the start of the line, so jumping to it works.
    for hit in hits:
        assert sample.read_lines_at(hit.offset, 1)[0].text == hit.text


def test_hits_carry_capture_groups(sample):
    matcher = Matcher(r"(\w+) after (\d+)ms", is_regex=True)
    hits, _ = run_search(sample, matcher)
    assert len(hits) == 1
    assert hits[0].groups == ("timeout", "30000")


def test_search_from_an_offset_skips_earlier_matches(sample):
    third = sample.offset_of_line(3)
    hits, _ = run_search(sample, Matcher("ERROR"), start_offset=third)
    assert len(hits) == 1
    assert "timeout" in hits[0].text


def test_search_respects_a_limit(sample):
    hits, result = run_search(sample, Matcher("2024"), limit=3)
    assert len(hits) == 3
    assert result["completed"] is False


def test_search_with_no_matches_completes(sample):
    hits, result = run_search(sample, Matcher("nothing matches this"))
    assert hits == []
    assert result["total"] == 0
    assert result["completed"] is True


def test_invalid_pattern_finishes_immediately(sample):
    hits, result = run_search(sample, Matcher("(unclosed", is_regex=True))
    assert hits == []
    assert result["total"] == 0


def test_match_spanning_a_chunk_boundary_is_found(tmp_path):
    # The scanner reads in INDEX_CHUNK blocks; a line straddling that edge is
    # the classic thing a chunked scanner gets wrong.
    filler = b"padding line that is reasonably long to move the boundary\n"
    body = filler * ((INDEX_CHUNK // len(filler)) + 2)
    marker_at = len(body)
    body += b"NEEDLE on its own line\n" + filler * 10
    path = tmp_path / "boundary.log"
    path.write_bytes(body)

    with LineFile(str(path)) as lf:
        hits, result = run_search(lf, Matcher("NEEDLE"))
        assert len(hits) == 1
        assert hits[0].offset == marker_at
        assert hits[0].text == "NEEDLE on its own line"


def test_line_straddling_the_chunk_edge_is_not_split(tmp_path):
    # Place a single line so it begins just before the chunk edge and ends
    # after it; iter_lines must yield it whole.
    prefix = b"x" * (INDEX_CHUNK - 20)
    body = prefix + b"START-marker-END so it crosses\n" + b"after\n"
    path = tmp_path / "straddle.log"
    path.write_bytes(body)
    with LineFile(str(path)) as lf:
        texts = [line.text for line in lf.iter_lines(0)]
        assert any(t.endswith("START-marker-END so it crosses") for t in texts)
        assert texts[-1] == "after"


def test_starting_a_search_cancels_the_previous_one(tmp_path):
    body = b"".join(b"line %d target\n" % i for i in range(200000))
    path = tmp_path / "long.log"
    path.write_bytes(body)

    with LineFile(str(path)) as lf:
        engine = SearchEngine(lf, on_hits=lambda batch: None,
                              on_done=lambda total, ok: None)
        engine.start(Matcher("target"))
        engine.start(Matcher("target"))  # supersedes the first
        engine.cancel()
        assert not engine.running


# ----------------------------------------------------------------------
# Find next
# ----------------------------------------------------------------------

def test_find_next_moves_forward(sample):
    matcher = Matcher("ERROR")
    first = find_next(sample, matcher, 0)
    assert "disk full" in first.text
    second = find_next(sample, matcher, first.offset)
    assert "timeout" in second.text
    assert find_next(sample, matcher, second.offset) is None


def test_find_next_backwards(sample):
    matcher = Matcher("ERROR")
    last_offset = sample.offset_of_line(5)
    found = find_next(sample, matcher, last_offset, backwards=True)
    assert "timeout" in found.text
    earlier = find_next(sample, matcher, found.offset, backwards=True)
    assert "disk full" in earlier.text
    assert find_next(sample, matcher, earlier.offset, backwards=True) is None


def test_find_next_with_inactive_matcher(sample):
    assert find_next(sample, Matcher(""), 0) is None


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------

def test_include_filter_keeps_only_matches(sample):
    kept = [line.text for line in iter_filtered(sample, Matcher("ERROR"))]
    assert len(kept) == 2
    assert all("ERROR" in text for text in kept)


def test_exclude_filter_drops_matches(sample):
    kept = [line.text for line in iter_filtered(sample, Matcher("INFO"),
                                                mode=FilterMode.EXCLUDE)]
    assert len(kept) == 4
    assert not any("INFO" in text for text in kept)


def test_filtered_lines_keep_their_real_offsets(sample):
    for line in iter_filtered(sample, Matcher("ERROR")):
        # Offsets must still address the original file, so double-clicking a
        # filtered line can jump the main view to it.
        assert sample.read_lines_at(line.offset, 1)[0].text == line.text


# ----------------------------------------------------------------------
# Highlight rules
# ----------------------------------------------------------------------

def test_first_matching_rule_wins():
    rules = RuleSet([
        HighlightRule("ERROR", bg="#FF0000"),
        HighlightRule("disk", bg="#00FF00"),
    ])
    # The line contains both; the earlier rule decides.
    assert rules.match("ERROR disk full").bg == "#FF0000"
    rules.move(1, -1)
    assert rules.match("ERROR disk full").bg == "#00FF00"


def test_disabled_rule_falls_through_to_the_next():
    rules = RuleSet([
        HighlightRule("ERROR", bg="#FF0000", enabled=False),
        HighlightRule("disk", bg="#00FF00"),
    ])
    assert rules.match("ERROR disk full").bg == "#00FF00"


def test_master_switch_disables_all_highlighting():
    rules = RuleSet([HighlightRule("ERROR")], enabled=False)
    assert rules.match("ERROR") is None
    assert rules.match_index("ERROR") == -1


def test_no_match_returns_none():
    assert RuleSet([HighlightRule("ERROR")]).match("all is well") is None


def test_broken_regex_rule_matches_nothing_but_is_kept():
    rule = HighlightRule("(unclosed", is_regex=True)
    assert not rule.matches("anything")
    assert not rule.valid
    assert rule.error
    # Still present, because the user is probably mid-edit.
    assert RuleSet([rule, HighlightRule("ok")]).match("ok") is not None


def test_rule_recompiles_when_edited():
    rule = HighlightRule("first")
    assert rule.matches("first")
    rule.pattern = "second"
    assert not rule.matches("first")
    assert rule.matches("second")


def test_move_clamps_at_the_ends():
    rules = RuleSet([HighlightRule("a"), HighlightRule("b")])
    assert rules.move(0, -1) == 0
    assert rules.move(1, 5) == 1


def test_rules_round_trip_through_dicts():
    original = RuleSet(list(DEFAULT_RULES))
    restored = RuleSet.from_list(original.to_list())
    assert [r.pattern for r in restored] == [r.pattern for r in original]
    assert [r.bg for r in restored] == [r.bg for r in original]
    assert [r.enabled for r in restored] == [r.enabled for r in original]


def test_default_rules_order_severity_first():
    # "ERROR: warning suppressed" must colour as an error, not a warning.
    rules = RuleSet(list(DEFAULT_RULES))
    assert rules.match("ERROR: warning suppressed").pattern == "ERROR"
