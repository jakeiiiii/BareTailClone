"""The scrolling log viewport.

This is the component the whole design rests on.  A ``tk.Text`` widget cannot
hold a multi-gigabyte file, so it never sees one: the widget contains only the
lines currently on screen -- typically fewer than a hundred -- and is refilled
from scratch whenever the view moves.

Two consequences follow, and they are what make tkinter viable here:

* Repainting costs the same for a 4 KB file and a 40 GB one.
* The scroll position is a **byte offset**, not a line number, so jumping to an
  arbitrary point never requires having counted the lines before it.

Because the widget holds only a fragment, its own scrollbar would describe the
fragment rather than the file.  The vertical scrollbar is therefore driven by
this class directly instead of through ``yscrollcommand``.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

from ..core import encoding as enc
from ..core.highlight import DEFAULT_COLOURS, RuleSet
from ..core.linefile import Line, LineFile
from .tkutil import release_font, view_font

__all__ = ["VirtualTextView"]

#: Logical lines scrolled per wheel notch, matching the Windows default.
WHEEL_LINES = 3

#: Upper bound on a whole-file copy, to keep an accidental Ctrl+A on a 40 GB
#: log from trying to build a 40 GB string.
MAX_COPY_BYTES = 64 << 20


class VirtualTextView(ttk.Frame):
    """A read-only view onto a :class:`~..core.linefile.LineFile`.

    The public surface is deliberately in file terms -- offsets, lines, "go to
    the end" -- so callers never deal with widget indices.
    """

    def __init__(self, master, linefile: LineFile | None = None,
                 ruleset: RuleSet | None = None,
                 font_family: str = "Consolas", font_size: int = 10,
                 wrap: bool = False, tab_width: int = 8,
                 line_spacing: int = 0, line_offset: int = 0,
                 on_position_change: Callable[[], None] | None = None) -> None:
        super().__init__(master)

        self._file = linefile
        self._rules = ruleset if ruleset is not None else RuleSet()
        self._on_position_change = on_position_change

        self._top = 0
        self._visible = 1
        self._lines: list[Line] = []
        self._follow = False
        self._wrap = wrap
        self._tab_width = tab_width
        self._marked_offset: int | None = None
        self._tag_signature: list[tuple] = []

        # view_font, not tkfont.Font: a Font finalised on a worker thread
        # calls Tcl from that thread and hangs it.  See ui/tkutil.py.
        self._font = view_font(family=font_family, size=font_size)

        self.text = tk.Text(
            self, wrap="char" if wrap else "none", font=self._font,
            undo=False, autoseparators=False, exportselection=True,
            background=DEFAULT_COLOURS["bg"], foreground=DEFAULT_COLOURS["fg"],
            insertwidth=0, padx=2, pady=0, borderwidth=0,
            highlightthickness=0, spacing1=line_offset, spacing3=line_spacing,
            state="disabled", cursor="arrow", takefocus=True,
        )
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._on_vscroll)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(xscrollcommand=self._on_xscroll)

        self.text.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._apply_wrap()

        self._bind_events()
        self._sync_tags()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _bind_events(self) -> None:
        self.text.bind("<Configure>", self._on_configure)
        # The Text widget's own scrolling would move within the loaded
        # fragment; every navigation key is handled here instead and the
        # default is suppressed with "break".
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.text.bind("<Button-4>", lambda e: self._wheel_scroll(-WHEEL_LINES))
        self.text.bind("<Button-5>", lambda e: self._wheel_scroll(WHEEL_LINES))
        for sequence, handler in (
            ("<Up>", lambda e: self._key_scroll(-1)),
            ("<Down>", lambda e: self._key_scroll(1)),
            ("<Prior>", lambda e: self._key_scroll(-self._page)),
            ("<Next>", lambda e: self._key_scroll(self._page)),
            ("<Control-Home>", lambda e: self._key_goto(self.goto_start)),
            ("<Control-End>", lambda e: self._key_goto(self.goto_end)),
            ("<Home>", lambda e: self._key_goto(self.goto_start)),
            ("<End>", lambda e: self._key_goto(self.goto_end)),
        ):
            self.text.bind(sequence, handler)
        self.text.bind("<Control-a>", self._on_select_all)
        self.text.bind("<Control-c>", lambda e: (self.copy_selection(), "break")[1])
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())

    def _on_xscroll(self, first, last) -> None:
        self.hsb.set(first, last)

    @property
    def _page(self) -> int:
        """Lines moved by PageUp/PageDown, keeping one line of context."""
        return max(1, self._visible - 1)

    # ------------------------------------------------------------------
    # File binding
    # ------------------------------------------------------------------

    def set_file(self, linefile: LineFile | None) -> None:
        self._file = linefile
        self._top = linefile.content_start if linefile else 0
        self._marked_offset = None
        self.render()

    @property
    def linefile(self) -> LineFile | None:
        return self._file

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
        # Row height changed, so the number of lines that fit has too.
        self._recalc_visible()
        self.render()

    def set_wrap(self, wrap: bool) -> None:
        self._wrap = wrap
        self._apply_wrap()
        self.render()

    def _apply_wrap(self) -> None:
        self.text.configure(wrap="char" if self._wrap else "none")
        # A wrapped view has nothing to scroll horizontally.
        if self._wrap:
            self.hsb.grid_remove()
        else:
            self.hsb.grid()

    def set_tab_width(self, width: int) -> None:
        self._tab_width = width
        self.render()

    def set_colours(self, fg: str, bg: str) -> None:
        self.text.configure(foreground=fg, background=bg)

    def _sync_tags(self) -> None:
        """Create one Text tag per rule.

        Tags are keyed by rule index and configured only when the rules
        actually change, so a repaint just applies existing tags rather than
        reconfiguring them per line.
        """
        signature = [(r.fg, r.bg) for r in self._rules]
        if signature == self._tag_signature:
            return

        for name in self.text.tag_names():
            if name.startswith("hl"):
                self.text.tag_delete(name)
        for index, rule in enumerate(self._rules):
            self.text.tag_configure(f"hl{index}", foreground=rule.fg, background=rule.bg)
        self.text.tag_configure("marked", underline=True)
        self._tag_signature = signature

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _on_configure(self, event=None) -> None:
        if not self._recalc_visible():
            return
        # A taller window shows more lines, and while following those must be
        # the lines at the *end*.  Re-rendering without re-anchoring would
        # leave the view stranded partway up the file after every resize --
        # and on first layout, where the view is packed at a height of one
        # line and only afterwards given its real size.
        if self._follow and self._file is not None:
            self._top = self._file.offset_of_last_page(self._visible)
        self.render()

    def _recalc_visible(self) -> bool:
        """Recompute how many lines fit, returning True if it changed."""
        height = self.text.winfo_height()
        row = self._row_height()
        visible = max(1, height // row) if height > 1 else 1
        if visible != self._visible:
            self._visible = visible
            return True
        return False

    def _row_height(self) -> int:
        spacing = int(self.text.cget("spacing1") or 0) + int(self.text.cget("spacing3") or 0)
        return max(1, self._font.metrics("linespace") + spacing)

    @property
    def visible_lines(self) -> int:
        return self._visible

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        """Refill the widget from the file at the current offset.

        Reads and inserts only ``visible_lines`` lines, which is what keeps
        this independent of file size.
        """
        text = self.text
        if self._file is None:
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.configure(state="disabled")
            self.vsb.set(0.0, 1.0)
            return

        self._sync_tags()
        # One extra line so a partially visible bottom row is not blank.
        lines = self._file.read_lines_at(self._top, self._visible + 1)
        self._lines = lines

        # Preserve the horizontal position; refilling would otherwise snap the
        # view back to column zero on every tail update.
        xview = text.xview()[0]

        text.configure(state="normal")
        text.delete("1.0", "end")
        if lines:
            width = self._tab_width
            body = "\n".join(enc.expand_tabs(line.text, width) for line in lines)
            text.insert("1.0", body)

            for row, line in enumerate(lines, start=1):
                index = self._rules.match_index(line.text)
                if index >= 0:
                    text.tag_add(f"hl{index}", f"{row}.0", f"{row}.0 lineend+1c")
                if self._marked_offset is not None and line.offset == self._marked_offset:
                    text.tag_add("marked", f"{row}.0", f"{row}.0 lineend")
        text.configure(state="disabled")

        if not self._wrap:
            text.xview_moveto(xview)
        self._update_scrollbar()
        if self._on_position_change is not None:
            self._on_position_change()

    def _update_scrollbar(self) -> None:
        """Drive the scrollbar from byte offsets.

        Proportional to bytes rather than lines, which is what lets the thumb
        be positioned correctly in a file whose lines have never been counted.
        """
        if self._file is None:
            self.vsb.set(0.0, 1.0)
            return

        size = self._file.size
        content = self._file.content_start
        span = max(1, size - content)
        first = (self._top - content) / span

        if self._lines:
            last_line = self._lines[-1]
            end = last_line.offset + max(1, len(last_line.text))
        else:
            end = size
        last = min(1.0, (end - content) / span)
        first = min(max(0.0, first), 1.0)
        self.vsb.set(first, max(last, first))

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _on_vscroll(self, *args) -> None:
        """Handle the scrollbar, translating its fraction into a byte offset."""
        if self._file is None or not args:
            return
        action = args[0]

        if action == "moveto":
            content = self._file.content_start
            span = max(1, self._file.size - content)
            target = content + int(float(args[1]) * span)
            self.set_top(target, follow_off=True)
        elif action == "scroll":
            amount = int(args[1])
            unit = args[2] if len(args) > 2 else "units"
            step = amount * (self._page if unit == "pages" else 1)
            self.scroll_lines(step)

    def _on_wheel(self, event) -> str:
        notches = -event.delta // 120 if event.delta else 0
        self._wheel_scroll(notches * WHEEL_LINES)
        return "break"

    def _wheel_scroll(self, lines: int) -> str:
        self.scroll_lines(lines)
        return "break"

    def _key_scroll(self, lines: int) -> str:
        self.scroll_lines(lines)
        return "break"

    def _key_goto(self, action: Callable[[], None]) -> str:
        action()
        return "break"

    def scroll_lines(self, delta: int) -> None:
        """Move the viewport by whole lines."""
        if self._file is None or delta == 0:
            return
        # Scrolling away from the end is how the user cancels follow mode,
        # exactly as it works in the original.
        if delta < 0:
            self.set_follow(False)
        self.set_top(self._file.step_lines(self._top, delta), follow_off=False)

    def set_top(self, offset: int, follow_off: bool = True) -> None:
        """Put the line containing ``offset`` at the top of the view."""
        if self._file is None:
            return
        if follow_off:
            self.set_follow(False)
        snapped = self._file.line_start_at_or_before(offset)
        # Never scroll past the point where the last line sits at the bottom.
        limit = self._file.offset_of_last_page(self._visible)
        self._top = min(snapped, limit) if snapped > limit else snapped
        self.render()

    def goto_start(self) -> None:
        self.set_follow(False)
        if self._file is not None:
            self._top = self._file.content_start
            self.render()

    def goto_end(self) -> None:
        """Scroll so the final line is visible, and resume following."""
        if self._file is None:
            return
        self._top = self._file.offset_of_last_page(self._visible)
        self.set_follow(True)
        self.render()

    def goto_offset(self, offset: int, centre: bool = True) -> None:
        """Bring ``offset`` into view, marking its line.

        Used when jumping to a search hit; centring keeps the surrounding
        context visible rather than pinning the hit to the top edge.
        """
        if self._file is None:
            return
        self.set_follow(False)
        start = self._file.line_start_at_or_before(offset)
        self._marked_offset = start
        top = self._file.step_lines(start, -(self._visible // 2)) if centre else start
        self._top = top
        self.render()

    def goto_line(self, number: int) -> None:
        if self._file is not None:
            self.goto_offset(self._file.offset_of_line(number))

    def clear_mark(self) -> None:
        self._marked_offset = None
        self.render()

    # ------------------------------------------------------------------
    # Follow tail
    # ------------------------------------------------------------------

    def set_follow(self, follow: bool) -> None:
        if self._follow != follow:
            self._follow = follow
            if follow:
                self.on_file_grown()

    @property
    def following(self) -> bool:
        return self._follow

    def on_file_grown(self) -> None:
        """Re-anchor to the end of the file, if following.

        Called by the tailer whenever the file changes size.  When not
        following, the scrollbar still has to be refreshed because the file
        being longer changes what the current position means.
        """
        if self._file is None:
            return
        if self._follow:
            self._top = self._file.offset_of_last_page(self._visible)
            self.render()
        else:
            self._update_scrollbar()

    def on_file_reset(self) -> None:
        """Re-anchor after truncation or rotation."""
        if self._file is None:
            return
        self._marked_offset = None
        self._top = (self._file.offset_of_last_page(self._visible)
                     if self._follow else self._file.content_start)
        self.render()

    # ------------------------------------------------------------------
    # Position reporting
    # ------------------------------------------------------------------

    @property
    def top_offset(self) -> int:
        return self._top

    @property
    def top_line_number(self) -> int:
        """0-based line number at the top of the view."""
        return self._file.line_number_at(self._top) if self._file else 0

    @property
    def rendered_lines(self) -> list[Line]:
        return self._lines

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def _on_select_all(self, event=None) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def copy_selection(self) -> str:
        """Copy the selected text, or the whole visible window if none."""
        try:
            data = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            data = self.text.get("1.0", "end-1c")
        if data:
            self.clipboard_clear()
            self.clipboard_append(data)
        return data

    def destroy(self) -> None:
        """Release the Tcl font here, on the main thread, while we still can."""
        release_font(self._font)
        self._font = None
        super().destroy()

    def copy_all(self) -> tuple[bool, str]:
        """Copy the entire file, streaming it rather than rendering it.

        Returns ``(ok, message)``; refuses beyond :data:`MAX_COPY_BYTES`
        rather than attempting to build a string of arbitrary size.
        """
        if self._file is None:
            return False, "No file open"
        size = self._file.size - self._file.content_start
        if size > MAX_COPY_BYTES:
            return False, (f"File is {size / (1 << 20):.0f} MB; copying more than "
                           f"{MAX_COPY_BYTES >> 20} MB at once is not supported.")

        parts: list[str] = []
        offset = self._file.content_start
        while offset < self._file.size:
            batch = self._file.read_lines_at(offset, 5000)
            if not batch:
                break
            parts.extend(line.text for line in batch)
            last = batch[-1]
            offset = last.offset + len(last.text.encode(self._file.codec.python_name, "replace")) + 1
        data = "\n".join(parts)
        self.clipboard_clear()
        self.clipboard_append(data)
        return True, f"Copied {len(parts)} lines"
