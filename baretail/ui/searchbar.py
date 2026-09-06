"""Incremental search and filter, with a results table.

This is the BareTailPro half of the feature set:

* typing searches as you go, reporting a regex's syntax error next to the
  field instead of failing silently;
* results stream into a sortable table showing timestamp, line number, text
  and any regex capture groups;
* double-clicking a result jumps the main view to that line;
* filter-tail shows only the lines that match, or only those that do not.

Results arrive from a worker thread and are posted through a
:class:`~.pump.UiPump`, so nothing here is called off the main thread.
"""

from __future__ import annotations

import csv
import io
import time
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Callable

from ..core.linefile import LineFile
from ..core.search import FilterMode, Matcher, SearchEngine, SearchHit, find_next

__all__ = ["SearchPanel"]

#: Cap on retained results.  A pattern matching most lines of a 60-million-line
#: file would otherwise exhaust memory building a table nobody can read.
MAX_RESULTS = 100000

_BASE_COLUMNS = ("time", "line", "text")


class SearchPanel(ttk.Frame):
    """The find bar and its results table.

    Owns its :class:`SearchEngine`; the main window supplies the current file,
    a way to jump to an offset, and the pump to marshal thread callbacks.
    """

    def __init__(self, master,
                 get_file: Callable[[], LineFile | None],
                 on_goto: Callable[[int], None],
                 pump,
                 on_filter: Callable[[Matcher | None, str], None] | None = None,
                 on_close: Callable[[], None] | None = None,
                 saved_patterns: list | None = None,
                 on_save_patterns: Callable[[list], None] | None = None) -> None:
        super().__init__(master)
        self._get_file = get_file
        self._on_goto = on_goto
        self._pump = pump
        self._on_filter = on_filter
        self._on_close = on_close
        self._on_save_patterns = on_save_patterns

        self._engine: SearchEngine | None = None
        self._hits: list[SearchHit] = []
        self._matcher: Matcher | None = None
        self._group_columns = 0
        self._sort_column: str | None = None
        self._sort_reverse = False
        self._pending_search: str | None = None
        self._truncated = False

        self._saved = list(saved_patterns or [])

        self._build()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = ttk.Frame(self, padding=(4, 4))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Find:").grid(row=0, column=0, padx=(0, 4))

        self._pattern = tk.StringVar()
        self._pattern.trace_add("write", lambda *a: self._on_pattern_changed())
        self._entry = ttk.Combobox(bar, textvariable=self._pattern,
                                   values=[p.get("pattern", "") for p in self._saved])
        self._entry.grid(row=0, column=1, sticky="ew")
        self._entry.bind("<Return>", lambda e: self.find_next())
        self._entry.bind("<Shift-Return>", lambda e: self.find_next(backwards=True))
        self._entry.bind("<Escape>", lambda e: self.close())

        options = ttk.Frame(bar)
        options.grid(row=0, column=2, padx=(6, 0))
        self._regex = tk.BooleanVar()
        self._case = tk.BooleanVar()
        self._word = tk.BooleanVar()
        for text, var in (("Regex", self._regex), ("Case", self._case),
                          ("Word", self._word)):
            ttk.Checkbutton(options, text=text, variable=var,
                            command=self._on_pattern_changed).pack(side="left")

        actions = ttk.Frame(bar)
        actions.grid(row=0, column=3, padx=(6, 0))
        ttk.Button(actions, text="◀", width=3,
                   command=lambda: self.find_next(backwards=True)).pack(side="left")
        ttk.Button(actions, text="▶", width=3,
                   command=self.find_next).pack(side="left", padx=(2, 6))
        ttk.Button(actions, text="Find All", width=9,
                   command=self.find_all).pack(side="left")

        filters = ttk.Frame(bar)
        filters.grid(row=0, column=4, padx=(6, 0))
        self._filter_mode = tk.StringVar(value="off")
        for text, value in (("No filter", "off"), ("Include", FilterMode.INCLUDE),
                            ("Exclude", FilterMode.EXCLUDE)):
            ttk.Radiobutton(filters, text=text, value=value,
                            variable=self._filter_mode,
                            command=self._on_filter_changed).pack(side="left")

        ttk.Button(bar, text="✕", width=3,
                   command=self.close).grid(row=0, column=5, padx=(6, 0))

        self._status = ttk.Label(bar, text="", foreground="#606060")
        self._status.grid(row=1, column=0, columnspan=6, sticky="w", pady=(2, 0))

        self._build_results()

    def _build_results(self) -> None:
        frame = ttk.Frame(self)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(frame, columns=_BASE_COLUMNS, show="headings",
                                  selectmode="browse", height=8)
        self._configure_columns()
        self._tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(xscrollcommand=xscroll.set)
        xscroll.grid(row=1, column=0, sticky="ew")

        self._tree.bind("<Double-1>", self._on_activate)
        self._tree.bind("<Return>", self._on_activate)

        buttons = ttk.Frame(frame, padding=(4, 4))
        buttons.grid(row=0, column=2, sticky="ns")
        for text, command in (("Save pattern", self._save_pattern),
                              ("Export…", self.export),
                              ("Copy", self.copy_results),
                              ("Clear", self.clear)):
            ttk.Button(buttons, text=text, width=13, command=command).pack(pady=2)

    def _configure_columns(self) -> None:
        """Rebuild the column set for the current number of capture groups."""
        columns = list(_BASE_COLUMNS) + [f"g{i}" for i in range(1, self._group_columns + 1)]
        self._tree.configure(columns=columns)

        headings = {"time": ("Time", 90), "line": ("Line", 90), "text": ("Text", 640)}
        for name in columns:
            title, width = headings.get(name, (f"Group {name[1:]}", 120))
            self._tree.heading(name, text=title,
                               command=lambda c=name: self._sort_by(c))
            anchor = "e" if name == "line" else "w"
            self._tree.column(name, width=width, anchor=anchor,
                              stretch=(name == "text"))

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------

    def _build_matcher(self) -> Matcher:
        return Matcher(self._pattern.get(), is_regex=self._regex.get(),
                       case_sensitive=self._case.get(), whole_word=self._word.get())

    def _on_pattern_changed(self) -> None:
        """React to a keystroke.

        The search itself is debounced -- restarting a scan over a multi-
        gigabyte file on every character would queue far more work than it
        completes -- but the pattern is validated immediately so a regex error
        is visible as it is typed.
        """
        matcher = self._build_matcher()
        self._matcher = matcher
        if matcher.error:
            self._status.configure(text=f"Invalid regular expression: {matcher.error}",
                                   foreground="#CC3333")
        else:
            self._status.configure(text="", foreground="#606060")

        if self._filter_mode.get() != "off":
            self._schedule(self._apply_filter)
        else:
            self._schedule(self.find_all)

    def _schedule(self, action: Callable[[], None], delay_ms: int = 250) -> None:
        if self._pending_search is not None:
            try:
                self.after_cancel(self._pending_search)
            except tk.TclError:
                pass
        self._pending_search = self.after(delay_ms, lambda: self._run(action))

    def _run(self, action: Callable[[], None]) -> None:
        self._pending_search = None
        action()

    def find_all(self) -> None:
        """Scan the whole file, streaming results into the table."""
        linefile = self._get_file()
        matcher = self._build_matcher()
        self._matcher = matcher
        self.clear()

        if linefile is None or not matcher.active:
            return

        self._group_columns = matcher.group_count
        self._configure_columns()
        self._status.configure(text="Searching…", foreground="#606060")

        self._stop_engine()
        self._engine = SearchEngine(
            linefile,
            on_hits=lambda batch: self._pump.post(self._add_hits, batch),
            on_done=lambda total, ok: self._pump.post(self._search_done, total, ok),
        )
        self._engine.start(matcher, start_offset=linefile.content_start)

    def find_next(self, backwards: bool = False) -> None:
        """Jump to the next match without building a table."""
        linefile = self._get_file()
        matcher = self._build_matcher()
        if linefile is None or not matcher.active:
            return

        origin = self._current_offset()
        found = find_next(linefile, matcher, origin, backwards=backwards)
        if found is None:
            # Wrap around, which is what a log viewer's Find Next is expected
            # to do rather than simply stopping at the end.
            edge = linefile.size if backwards else linefile.content_start - 1
            found = find_next(linefile, matcher, edge, backwards=backwards)
            if found is None:
                self._status.configure(text="No matches", foreground="#606060")
                return
            self._status.configure(text="Wrapped around", foreground="#606060")
        else:
            self._status.configure(text="", foreground="#606060")
        self._on_goto(found.offset)

    def _current_offset(self) -> int:
        selection = self._tree.selection()
        if selection:
            values = self._tree.item(selection[0], "values")
            if values:
                return int(self._tree.item(selection[0], "tags")[0])
        linefile = self._get_file()
        return getattr(self, "_last_goto", linefile.content_start if linefile else 0)

    def _stop_engine(self) -> None:
        if self._engine is not None:
            self._engine.cancel()
            self._engine = None

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _add_hits(self, batch: list[SearchHit]) -> None:
        if len(self._hits) >= MAX_RESULTS:
            self._truncated = True
            return

        room = MAX_RESULTS - len(self._hits)
        if len(batch) > room:
            batch = batch[:room]
            self._truncated = True

        for hit in batch:
            self._hits.append(hit)
            self._tree.insert("", "end", values=self._row(hit), tags=(str(hit.offset),))
        self._status.configure(
            text=f"{len(self._hits):,} match{'es' if len(self._hits) != 1 else ''}…",
            foreground="#606060")

    def _row(self, hit: SearchHit) -> tuple:
        stamp = time.strftime("%H:%M:%S", time.localtime(hit.timestamp))
        number = f"{hit.line_number + 1:,}" if hit.line_number is not None else ""
        groups = list(hit.groups[: self._group_columns])
        groups += [""] * (self._group_columns - len(groups))
        return (stamp, number, hit.text, *groups)

    def _search_done(self, total: int, completed: bool) -> None:
        shown = len(self._hits)
        if self._truncated:
            text = (f"{shown:,} matches shown; stopped at the {MAX_RESULTS:,} "
                    f"result limit")
        elif completed:
            text = f"{shown:,} match{'es' if shown != 1 else ''}"
        else:
            text = f"{shown:,} matches (stopped early)"
        self._status.configure(text=text, foreground="#606060")

    def clear(self) -> None:
        self._stop_engine()
        self._hits.clear()
        self._truncated = False
        self._tree.delete(*self._tree.get_children())
        self._status.configure(text="", foreground="#606060")

    def _on_activate(self, event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        tags = self._tree.item(selection[0], "tags")
        if tags:
            offset = int(tags[0])
            self._last_goto = offset
            self._on_goto(offset)

    def _sort_by(self, column: str) -> None:
        """Sort the table by a column, toggling direction on re-click."""
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False

        index = list(self._tree.cget("columns")).index(column)

        def key(item):
            value = self._tree.item(item, "values")[index]
            if column == "line":
                # Sort line numbers numerically; they are displayed with
                # thousands separators.
                try:
                    return (0, int(str(value).replace(",", "")))
                except ValueError:
                    return (1, 0)
            return (0, str(value).lower())

        items = sorted(self._tree.get_children(""), key=key, reverse=self._sort_reverse)
        for position, item in enumerate(items):
            self._tree.move(item, "", position)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _on_filter_changed(self) -> None:
        mode = self._filter_mode.get()
        if mode == "off":
            if self._on_filter is not None:
                self._on_filter(None, mode)
            return
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._on_filter is None:
            return
        mode = self._filter_mode.get()
        if mode == "off":
            self._on_filter(None, mode)
            return
        matcher = self._build_matcher()
        self._on_filter(matcher if matcher.active else None, mode)

    @property
    def filtering(self) -> bool:
        return self._filter_mode.get() != "off"

    # ------------------------------------------------------------------
    # Saved patterns and export
    # ------------------------------------------------------------------

    def _save_pattern(self) -> None:
        pattern = self._pattern.get()
        if not pattern:
            return
        entry = {"pattern": pattern, "regex": self._regex.get(),
                 "case_sensitive": self._case.get(), "whole_word": self._word.get()}
        self._saved = [p for p in self._saved if p.get("pattern") != pattern]
        self._saved.insert(0, entry)
        del self._saved[20:]
        self._entry.configure(values=[p.get("pattern", "") for p in self._saved])
        if self._on_save_patterns is not None:
            self._on_save_patterns(self._saved)
        self._status.configure(text=f"Saved pattern “{pattern}”", foreground="#606060")

    def results_as_text(self, delimiter: str = "\t") -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
        columns = list(self._tree.cget("columns"))
        writer.writerow(columns)
        for item in self._tree.get_children(""):
            writer.writerow(self._tree.item(item, "values"))
        return buffer.getvalue()

    def copy_results(self) -> None:
        if not self._hits:
            return
        self.clipboard_clear()
        self.clipboard_append(self.results_as_text())
        self._status.configure(text=f"Copied {len(self._hits):,} results",
                               foreground="#606060")

    def export(self) -> None:
        if not self._hits:
            self._status.configure(text="Nothing to export", foreground="#606060")
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export results", defaultextension=".txt",
            filetypes=[("Tab separated", "*.txt"), ("CSV", "*.csv"),
                       ("All files", "*.*")])
        if not path:
            return
        delimiter = "," if path.lower().endswith(".csv") else "\t"
        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(self.results_as_text(delimiter))
            self._status.configure(text=f"Exported to {path}", foreground="#606060")
        except OSError as exc:
            self._status.configure(text=f"Export failed: {exc}", foreground="#CC3333")

    # ------------------------------------------------------------------

    def focus_search(self) -> None:
        self._entry.focus_set()
        self._entry.selection_range(0, "end")

    def close(self) -> None:
        self._stop_engine()
        if self._on_close is not None:
            self._on_close()

    def destroy(self) -> None:
        self._stop_engine()
        super().destroy()
