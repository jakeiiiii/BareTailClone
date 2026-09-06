"""End-to-end checks that drive the real widgets.

These build an actual Tk window rather than mocking it, because the things
most likely to break -- a viewport that renders the wrong lines, a tab whose
view is never raised, a filter that shows nothing -- are exactly the things a
mock would happily accept.

Skipped where no display is available.
"""

from __future__ import annotations

import os
import time

import pytest

tk = pytest.importorskip("tkinter")

from baretail.config import Config, StorageMode          # noqa: E402
from baretail.core.highlight import HighlightRule, RuleSet   # noqa: E402
from baretail.core.linefile import LineFile              # noqa: E402
from baretail.core.search import FilterMode, Matcher     # noqa: E402
from baretail.ui.filterview import FilteredView          # noqa: E402
from baretail.ui.logview import VirtualTextView          # noqa: E402
from baretail.ui.mainwindow import MainWindow            # noqa: E402
from baretail.ui.tabstrip import Side, TabStatus, TabStrip    # noqa: E402


# Exactly one Tk interpreter exists for this whole module, and it is never
# destroyed.  Two separate reasons, both learned the hard way:
#
#  * Tcl finalises itself when the last Tk goes away, and creating another
#    afterwards fails in the same process with a misleading "Can't find a
#    usable init.tcl".
#  * Repeatedly creating and destroying secondary Tk interpreters crashes the
#    process outright, part-way through the run.
#
# MainWindow *is* a tk.Tk, so it serves as that single root: the plain-widget
# tests hang a Toplevel off it, and the application tests reuse it with their
# state reset rather than building a new window each time.
def _fresh_config(directory) -> Config:
    """A configuration that never writes anything anywhere."""
    config = Config(path=str(directory / "settings.json"))
    config.storage = StorageMode.NONE
    return config


try:
    _APP = MainWindow(_fresh_config(__import__("pathlib").Path(__import__("tempfile")
                                               .mkdtemp())))
except tk.TclError:
    pytest.skip("no display available", allow_module_level=True)
_APP.geometry("1000x700")
_APP.update()


@pytest.fixture(scope="module")
def root():
    window = tk.Toplevel(_APP)
    window.geometry("900x600")
    window.update()
    yield window
    window.destroy()


@pytest.fixture
def logfile(tmp_path):
    body = b"".join(b"2024-01-01 %-5s line %04d payload\n"
                    % (b"ERROR" if i % 10 == 0 else b"INFO", i)
                    for i in range(500))
    path = tmp_path / "view.log"
    path.write_bytes(body)
    return str(path)


def rendered(view) -> list[str]:
    """Text currently in the widget, as a list of lines."""
    return view.text.get("1.0", "end-1c").split("\n")


def pump_until(app, predicate, timeout: float = 10.0) -> bool:
    """Run the event loop until ``predicate`` holds, or time runs out.

    Results from search and filtering arrive on a worker thread and are
    applied by a periodic drain, so the test has to let real time pass --
    calling update() in a tight loop spins the event loop without ever
    advancing the clock that drain is scheduled against.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------------------
# VirtualTextView
# ----------------------------------------------------------------------

def test_view_shows_the_top_of_the_file(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_start()
        root.update()

        lines = rendered(view)
        assert lines[0].endswith("line 0000 payload")
        # The whole point: the widget holds a screenful, not the file.
        assert len(lines) <= view.visible_lines + 2
        view.destroy()


def test_view_shows_the_end_when_following(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_end()
        root.update()

        assert rendered(view)[-1].endswith("line 0499 payload")
        assert view.following
        view.destroy()


def test_scrolling_back_stops_following(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_end()
        root.update()
        assert view.following

        view.scroll_lines(-5)
        root.update()
        # Scrolling away from the end is how the user detaches from the tail.
        assert not view.following
        view.destroy()


def test_scroll_positions_are_consistent(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_start()
        root.update()

        view.scroll_lines(10)
        root.update()
        assert rendered(view)[0].endswith("line 0010 payload")

        view.scroll_lines(-4)
        root.update()
        assert rendered(view)[0].endswith("line 0006 payload")
        view.destroy()


def test_view_does_not_scroll_past_the_end(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.scroll_lines(100000)
        root.update()
        # The last line stays visible rather than the view running off into
        # empty space beyond the file.
        assert rendered(view)[-1].endswith("line 0499 payload")
        view.destroy()


def test_goto_offset_brings_the_line_into_view(root, logfile):
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        target = lf.offset_of_line(300)
        view.goto_offset(target)
        root.update()
        assert any(line.endswith("line 0300 payload") for line in rendered(view))
        view.destroy()


def test_highlight_tags_are_applied_to_matching_lines(root, logfile):
    rules = RuleSet([HighlightRule("ERROR", fg="#FFFFFF", bg="#CC0000")])
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf, rules)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_start()
        root.update()

        tagged = view.text.tag_ranges("hl0")
        assert tagged, "no line was highlighted"
        # Every tagged range must actually be an ERROR line.
        for i in range(0, len(tagged), 2):
            text = view.text.get(tagged[i], tagged[i + 1])
            assert "ERROR" in text
        view.destroy()


def test_disabling_highlighting_removes_the_tags(root, logfile):
    rules = RuleSet([HighlightRule("ERROR", bg="#CC0000")])
    with LineFile(logfile) as lf:
        view = VirtualTextView(root, lf, rules)
        view.pack(fill="both", expand=True)
        root.update()
        assert view.text.tag_ranges("hl0")

        rules.enabled = False
        view.set_ruleset(rules)
        root.update()
        assert not view.text.tag_ranges("hl0")
        view.destroy()


def test_view_handles_an_empty_file(root, tmp_path):
    path = tmp_path / "empty.log"
    path.write_bytes(b"")
    with LineFile(str(path)) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_end()
        root.update()
        assert view.text.get("1.0", "end-1c") == ""
        view.destroy()


def test_view_reflects_growth_while_following(root, tmp_path):
    path = tmp_path / "grow.log"
    path.write_bytes(b"first\n")
    with LineFile(str(path)) as lf:
        view = VirtualTextView(root, lf)
        view.pack(fill="both", expand=True)
        root.update()
        view.goto_end()
        root.update()

        with open(path, "ab") as fh:
            fh.write(b"second\nthird\n")
        view.on_file_grown()
        root.update()
        assert rendered(view)[-1] == "third"
        view.destroy()


# ----------------------------------------------------------------------
# Tab strip
# ----------------------------------------------------------------------

def test_tabstrip_tracks_tabs_and_selection(root):
    strip = TabStrip(root)
    strip.pack()
    strip.add("a", "alpha.log")
    strip.add("b", "beta.log")
    root.update()

    assert strip.keys == ["a", "b"]
    assert strip.active == "b"
    strip.set_active("a")
    assert strip.active == "a"
    strip.destroy()


def test_closing_a_tab_activates_a_neighbour(root):
    strip = TabStrip(root)
    strip.pack()
    for key in ("a", "b", "c"):
        strip.add(key, key)
    strip.set_active("b")
    root.update()

    assert strip.remove("b") == "c"
    assert strip.keys == ["a", "c"]
    assert strip.remove("c") == "a"
    assert strip.remove("a") is None
    strip.destroy()


def test_selecting_a_tab_clears_its_change_marker(root):
    strip = TabStrip(root)
    strip.pack()
    strip.add("a", "alpha")
    strip.add("b", "beta")
    strip.set_status("a", TabStatus.CHANGED)
    root.update()

    strip.set_active("a")
    # Looking at the file is what acknowledges the change.
    assert strip.find_tab("a").status == TabStatus.OK
    strip.destroy()


def test_tabstrip_survives_every_side_and_orientation(root):
    strip = TabStrip(root)
    strip.pack()
    strip.add("a", "alpha.log")
    for side in (Side.TOP, Side.BOTTOM, Side.LEFT, Side.RIGHT):
        for orientation in ("horizontal", "vertical"):
            strip.set_side(side)
            strip.set_orientation(orientation)
            root.update()
    strip.destroy()


def test_relative_selection_wraps(root):
    strip = TabStrip(root)
    strip.pack()
    for key in ("a", "b", "c"):
        strip.add(key, key)
    strip.set_active("c")
    assert strip.select_relative(1) == "a"
    assert strip.select_relative(-1) == "c"
    strip.destroy()


# ----------------------------------------------------------------------
# Filtered view
# ----------------------------------------------------------------------

def test_filtered_view_shows_only_what_it_is_given(root, logfile):
    from baretail.core.search import iter_filtered

    with LineFile(logfile) as lf:
        view = FilteredView(root, show_timestamps=False)
        view.pack(fill="both", expand=True)
        root.update()

        kept = list(iter_filtered(lf, Matcher("ERROR"), FilterMode.INCLUDE))
        view.extend(kept)
        root.update()

        assert view.count == 50
        assert all("ERROR" in line for line in rendered(view) if line)
        view.destroy()


def test_filtered_view_keeps_offsets_for_jumping_back(root, logfile):
    from baretail.core.search import iter_filtered

    with LineFile(logfile) as lf:
        jumped = []
        view = FilteredView(root, on_activate=jumped.append)
        view.pack(fill="both", expand=True)
        root.update()
        kept = list(iter_filtered(lf, Matcher("ERROR"), FilterMode.INCLUDE))
        view.extend(kept)
        root.update()

        # The offset a filtered line carries must still address the real file.
        first = kept[0]
        assert lf.read_lines_at(first.offset, 1)[0].text == first.text
        view.destroy()


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """The shared MainWindow, reset to a clean state for each test.

    Reset rather than rebuilt: see the note above the module root.  Each test
    gets its own settings object, so nothing carries over except the window
    itself.
    """
    _APP.config_data = _fresh_config(tmp_path)
    _APP._rules = _APP.config_data.ruleset()

    _APP.clear_filter()
    _APP.hide_search()
    for key in list(_APP._tabs):
        _APP.close_tab(key)
    _APP._follow_var.set(True)
    _APP._wrap_var.set(False)
    _APP._highlight_var.set(True)
    _APP._toggle_wrap()
    _APP._refresh_views()
    _APP.update()

    yield _APP

    for key in list(_APP._tabs):
        _APP.close_tab(key)
    _APP.update()


def test_window_opens_a_file_into_a_tab(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    assert tab is not None
    assert app.active_tab is tab
    assert os.path.basename(logfile) in app.title()
    assert rendered(tab.view)[-1].endswith("line 0499 payload")


def test_opening_the_same_file_twice_reuses_the_tab(app, logfile):
    first = app.open_file(logfile)
    second = app.open_file(logfile)
    app.update()
    assert first is second
    assert len(app._tabs) == 1


def test_multiple_files_get_their_own_tabs(app, tmp_path, logfile):
    other = tmp_path / "other.log"
    other.write_bytes(b"other content\n")
    app.open_file(logfile)
    app.open_file(str(other))
    app.update()

    assert len(app._tabs) == 2
    assert app.active_tab.path == str(other)
    assert rendered(app.active_tab.view)[0] == "other content"


def test_switching_tabs_changes_the_visible_view(app, tmp_path, logfile):
    other = tmp_path / "other.log"
    other.write_bytes(b"other content\n")
    first = app.open_file(logfile)
    second = app.open_file(str(other))
    app.update()

    app._show_tab(first.key)
    app.update()
    assert first.view.winfo_ismapped()
    assert not second.view.winfo_ismapped()


def test_closing_a_tab_releases_the_file(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    key = tab.key
    app.close_tab(key)
    app.update()
    assert key not in app._tabs
    # The handle must be closed, or the file could not be rotated or deleted.
    assert tab.linefile._fh is None


def test_closing_the_last_tab_shows_the_placeholder(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    app.close_tab(tab.key)
    app.update()
    assert app.active_tab is None
    assert app.title() == "BareTail"


def test_toggling_follow_moves_to_the_end(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    tab.view.goto_start()
    app.update()
    assert not tab.view.following

    app._follow_var.set(True)
    app._toggle_follow()
    app.update()
    assert tab.view.following
    assert rendered(tab.view)[-1].endswith("line 0499 payload")


def test_highlight_toggle_reaches_every_open_view(app, tmp_path, logfile):
    other = tmp_path / "other.log"
    other.write_bytes(b"ERROR in other\n")
    first = app.open_file(logfile)
    second = app.open_file(str(other))
    app.update()

    app._highlight_var.set(False)
    app._toggle_highlighting()
    app.update()
    assert not first.view.text.tag_ranges("hl0")
    assert not second.view.text.tag_ranges("hl0")


def test_growth_marks_a_background_tab(app, tmp_path, logfile):
    other = tmp_path / "other.log"
    other.write_bytes(b"start\n")
    background = app.open_file(logfile)
    app.open_file(str(other))       # this one is active
    app.update()

    app._on_file_change(background.key, "grew", 999)
    app.update()
    # The whole point of the indicator: the user can see which of several
    # files moved without clicking through them.
    assert app._tabstrip.find_tab(background.key).status == TabStatus.CHANGED


def test_filter_shows_only_matching_lines(app, logfile):
    tab = app.open_file(logfile)
    app.update()

    app._set_filter(Matcher("ERROR"), FilterMode.INCLUDE)
    assert pump_until(app, lambda: tab.filter_view is not None
                      and tab.filter_view.count >= 50), "filter produced no results"

    assert tab.filtering
    assert tab.filter_view.count == 50
    assert tab.filter_view.winfo_ismapped()
    assert not tab.view.winfo_ismapped()


def test_exclude_filter_drops_matching_lines(app, logfile):
    tab = app.open_file(logfile)
    app.update()

    app._set_filter(Matcher("ERROR"), FilterMode.EXCLUDE)
    assert pump_until(app, lambda: tab.filter_view is not None
                      and tab.filter_view.count >= 450), "exclude filter produced too few"

    assert tab.filter_view.count == 450


def test_clearing_the_filter_restores_the_file_view(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    app._set_filter(Matcher("ERROR"), FilterMode.INCLUDE)
    app.update()
    app.clear_filter()
    app.update()

    assert not tab.filtering
    assert tab.view.winfo_ismapped()


def test_search_panel_finds_and_jumps(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    app.show_search()
    app.update()

    panel = app._search
    panel._pattern.set("line 0300")
    panel.find_next()
    app.update()

    assert any("line 0300" in line for line in rendered(tab.view))
    assert not tab.view.following


def test_search_reports_an_invalid_regex(app, logfile):
    app.open_file(logfile)
    app.update()
    app.show_search()
    panel = app._search
    panel._regex.set(True)
    panel._pattern.set("(unclosed")
    app.update()
    assert "Invalid regular expression" in panel._status.cget("text")


def test_find_all_populates_the_results_table(app, logfile):
    app.open_file(logfile)
    app.update()
    app.show_search()
    panel = app._search
    panel._pattern.set("ERROR")
    panel.find_all()

    assert pump_until(app, lambda: len(panel._tree.get_children()) >= 50),         "search results never arrived"
    assert len(panel._tree.get_children()) == 50


def test_goto_line_moves_the_view(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    tab.view.goto_line(250)
    app.update()
    assert any("line 0250" in line for line in rendered(tab.view))


def test_tab_placement_options_all_work(app, logfile):
    app.open_file(logfile)
    for side in (Side.TOP, Side.BOTTOM, Side.LEFT, Side.RIGHT):
        for orientation in ("horizontal", "vertical"):
            app._side_var.set(side)
            app._orientation_var.set(orientation)
            app._apply_tab_placement()
            app.update()
    assert app._tabstrip.side == Side.RIGHT


def test_wrap_toggle_applies_to_open_views(app, logfile):
    tab = app.open_file(logfile)
    app.update()
    app._wrap_var.set(True)
    app._toggle_wrap()
    app.update()
    assert tab.view.text.cget("wrap") == "char"
