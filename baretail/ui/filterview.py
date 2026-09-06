"""The filter-tail view.

Filtering shows only the lines that match a pattern (or only those that do
not), updating live as the file grows.  Those lines are scattered through the
file rather than contiguous, so :class:`~.logview.VirtualTextView` -- which
addresses the file by byte offset -- cannot render them.

This view keeps the surviving lines in a list and virtualises over *that*
instead.  The list is what filtering makes affordable: a pattern narrow enough
to be useful reduces a huge file to something that fits in memory, and the cap
in :data:`MAX_LINES` bounds the case where it does not.

Each retained line keeps its original byte offset, so double-clicking one can
still jump the unfiltered view to exactly that place in the file.
"""

from __future__ import annotations

import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

from ..core import encoding as enc
from ..core.highlight import DEFAULT_COLOURS, RuleSet
from ..core.linefile import Line

__all__ = ["FilteredView", "MAX_LINES"]

#: Most filtered lines retained.  Beyond this the oldest are dropped, which
#: for a live filter is the right end to lose -- the interesting lines in a
#: tail are the recent ones.
MAX_LINES = 200000


class FilteredView(ttk.Frame):
    """A scrollable view over a list of retained lines."""

    def __init__(self, master, ruleset: RuleSet | None = None,
                 font_family: str = "Consolas", font_size: int = 10,
                 wrap: bool = False, tab_width: int = 8,
                 show_timestamps: bool = True,
                 on_activate: Callable[[int], None] | None = None) -> None:
        super().__init__(master)
        self._rules = ruleset if ruleset is not None else RuleSet()
        self._on_activate = on_activate
        self._show_timestamps = show_timestamps
        self._tab_width = tab_width

        self._lines: list[Line] = []
        self._stamps: list[float] = []
        self._top = 0
        self._visible = 1
        self._follow = True
        self._dropped = 0
        self._tag_signature: list[tuple] = []

        self._font = tkfont.Font(family=font_family, size=font_size)
        self.text = tk.Text(self, wrap="char" if wrap else "none", font=self._font,
                            state="disabled", cursor="arrow", padx=2, pady=0,
                            borderwidth=0, highlightthickness=0, insertwidth=0,
                            background=DEFAULT_COLOURS["bg"],
                            foreground=DEFAULT_COLOURS["fg"])
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._on_vscroll)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(xscrollcommand=self.hsb.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.text.bind("<Configure>", self._on_configure)
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.text.bind("<Up>", lambda e: self._scroll(-1))
        self.text.bind("<Down>", lambda e: self._scroll(1))
        self.text.bind("<Prior>", lambda e: self._scroll(-self._visible))
        self.text.bind("<Next>", lambda e: self._scroll(self._visible))
        self.text.bind("<Double-1>", self._on_double_click)
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self._sync_tags()

    # ------------------------------------------------------------------
    # Contents
    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._lines.clear()
        self._stamps.clear()
        self._top = 0
        self._dropped = 0
        self._follow = True
        self.render()

    def extend(self, lines: list[Line]) -> None:
        """Append newly matched lines, dropping the oldest past the cap."""
        if not lines:
            return
        now = time.time()
        self._lines.extend(lines)
        self._stamps.extend([now] * len(lines))

        overflow = len(self._lines) - MAX_LINES
        if overflow > 0:
            del self._lines[:overflow]
            del self._stamps[:overflow]
            self._dropped += overflow
            self._top = max(0, self._top - overflow)

        if self._follow:
            self._top = max(0, len(self._lines) - self._visible)
        self.render()

    @property
    def count(self) -> int:
        return len(self._lines)

    @property
    def dropped(self) -> int:
        return self._dropped

    def set_follow(self, follow: bool) -> None:
        self._follow = follow
        if follow:
            self._top = max(0, len(self._lines) - self._visible)
            self.render()

    @property
    def following(self) -> bool:
        return self._follow

    # ------------------------------------------------------------------
    # Appearance
    # ------------------------------------------------------------------

    def set_ruleset(self, ruleset: RuleSet) -> None:
        self._rules = ruleset
        self._sync_tags()
        self.render()

    def set_font(self, family: str, size: int, spacing: int = 0, offset: int = 0) -> None:
        self._font.configure(family=family, size=size)
        self.text.configure(spacing1=offset, spacing3=spacing)
        self._recalc_visible()
        self.render()

    def set_wrap(self, wrap: bool) -> None:
        self.text.configure(wrap="char" if wrap else "none")
        if wrap:
            self.hsb.grid_remove()
        else:
            self.hsb.grid()
        self.render()

    def set_tab_width(self, width: int) -> None:
        self._tab_width = width
        self.render()

    def set_colours(self, fg: str, bg: str) -> None:
        self.text.configure(foreground=fg, background=bg)

    def set_show_timestamps(self, show: bool) -> None:
        self._show_timestamps = show
        self.render()

    def _sync_tags(self) -> None:
        signature = [(r.fg, r.bg) for r in self._rules]
        if signature == self._tag_signature:
            return
        for name in self.text.tag_names():
            if name.startswith("hl"):
                self.text.tag_delete(name)
        for index, rule in enumerate(self._rules):
            self.text.tag_configure(f"hl{index}", foreground=rule.fg, background=rule.bg)
        self.text.tag_configure("stamp", foreground="#808080")
        self._tag_signature = signature

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _on_configure(self, event=None) -> None:
        if self._recalc_visible():
            if self._follow:
                self._top = max(0, len(self._lines) - self._visible)
            self.render()

    def _recalc_visible(self) -> bool:
        height = self.text.winfo_height()
        spacing = int(self.text.cget("spacing1") or 0) + int(self.text.cget("spacing3") or 0)
        row = max(1, self._font.metrics("linespace") + spacing)
        visible = max(1, height // row) if height > 1 else 1
        if visible != self._visible:
            self._visible = visible
            return True
        return False

    def render(self) -> None:
        """Draw only the visible slice of the retained lines."""
        top = max(0, min(self._top, max(0, len(self._lines) - 1)))
        self._top = top
        window = self._lines[top: top + self._visible + 1]
        stamps = self._stamps[top: top + self._visible + 1]

        self._sync_tags()
        xview = self.text.xview()[0]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")

        prefixes = []
        for index, line in enumerate(window):
            # BareTailPro's filter view timestamps each line with when it
            # arrived, which is the only way to tell rate from a filtered feed.
            prefix = (time.strftime("%H:%M:%S ", time.localtime(stamps[index]))
                      if self._show_timestamps else "")
            prefixes.append(prefix)
            self.text.insert("end", prefix + enc.expand_tabs(line.text, self._tab_width))
            if index < len(window) - 1:
                self.text.insert("end", "\n")

        for index, line in enumerate(window):
            row = index + 1
            rule = self._rules.match_index(line.text)
            if rule >= 0:
                self.text.tag_add(f"hl{rule}", f"{row}.0", f"{row}.0 lineend+1c")
            if prefixes[index]:
                self.text.tag_add("stamp", f"{row}.0", f"{row}.{len(prefixes[index])}")

        self.text.configure(state="disabled")
        self.text.xview_moveto(xview)
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        total = max(1, len(self._lines))
        first = self._top / total
        last = min(1.0, (self._top + self._visible) / total)
        self.vsb.set(first, max(last, first))

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _on_vscroll(self, *args) -> None:
        if not args:
            return
        if args[0] == "moveto":
            self._goto(int(float(args[1]) * max(1, len(self._lines))))
        elif args[0] == "scroll":
            amount = int(args[1])
            unit = args[2] if len(args) > 2 else "units"
            self._scroll(amount * (self._visible if unit == "pages" else 1))

    def _on_wheel(self, event) -> str:
        self._scroll((-event.delta // 120) * 3 if event.delta else 0)
        return "break"

    def _scroll(self, delta: int) -> str:
        if delta:
            self._goto(self._top + delta)
        return "break"

    def _goto(self, index: int) -> None:
        limit = max(0, len(self._lines) - self._visible)
        target = max(0, min(index, limit))
        # Scrolling back detaches from the tail; returning to the bottom
        # reattaches, which is the same rule the main view uses.
        self._follow = target >= limit
        if target != self._top:
            self._top = target
            self.render()
        else:
            self._update_scrollbar()

    def _on_double_click(self, event) -> str:
        """Jump the unfiltered view to the line that was double-clicked."""
        if self._on_activate is None:
            return "break"
        row = int(self.text.index(f"@{event.x},{event.y}").split(".")[0]) - 1
        index = self._top + row
        if 0 <= index < len(self._lines):
            self._on_activate(self._lines[index].offset)
        return "break"
