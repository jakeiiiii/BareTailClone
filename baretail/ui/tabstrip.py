"""A tab strip that can sit on any edge of the window.

``ttk.Notebook`` is not usable here for two reasons.  Under the Windows
``vista`` theme it does not honour tab placement on all four sides, and it
offers nowhere to draw the per-tab status and change indicators that are one of
BareTail's distinguishing features -- the marker that tells you which of eight
monitored files just moved without having to click through them.

So the strip is drawn on a Canvas.  That also makes rotated text possible,
which is what lets a strip on the left or right edge be narrow instead of
consuming a column of window width.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

__all__ = ["TabStrip", "Side", "Orientation", "TabStatus"]


class Side:
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


class Orientation:
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


class TabStatus:
    OK = "ok"
    CHANGED = "changed"
    MISSING = "missing"
    ERROR = "error"


#: Indicator colours, matching what the status bar and menus use.
STATUS_COLOURS = {
    TabStatus.OK: None,             # no dot when nothing has happened
    TabStatus.CHANGED: "#2E9E48",
    TabStatus.MISSING: "#B0B0B0",
    TabStatus.ERROR: "#CC3333",
}

_PAD_X = 10
_PAD_Y = 5
_DOT = 7
_GAP = 2
_CLOSE = 14


class _Tab:
    __slots__ = ("key", "label", "status", "x", "y", "width", "height", "close_box")

    def __init__(self, key: str, label: str) -> None:
        self.key = key
        self.label = label
        self.status = TabStatus.OK
        self.x = self.y = self.width = self.height = 0
        self.close_box = (0, 0, 0, 0)


class TabStrip(tk.Canvas):
    """An ordered set of tabs drawn on one edge of the window.

    Exposes tabs by an opaque key -- the caller's own identifier for the file
    -- rather than by index, so reordering or closing a tab never invalidates a
    reference held elsewhere.
    """

    def __init__(self, master,
                 on_select: Callable[[str], None] | None = None,
                 on_close: Callable[[str], None] | None = None,
                 side: str = Side.TOP,
                 orientation: str = Orientation.HORIZONTAL,
                 font_family: str = "Segoe UI", font_size: int = 9) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0,
                         background="#F0F0F0")
        self._on_select = on_select
        self._on_close = on_close
        self._side = side
        self._orientation = orientation
        self._font = tkfont.Font(family=font_family, size=font_size)

        self._tabs: list[_Tab] = []
        self._active: str | None = None
        self._hover: str | None = None

        self.bind("<Button-1>", self._on_click)
        self.bind("<Button-2>", self._on_middle_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda e: self._redraw())

    # ------------------------------------------------------------------
    # Contents
    # ------------------------------------------------------------------

    def add(self, key: str, label: str, activate: bool = True) -> None:
        if self.find_tab(key) is None:
            self._tabs.append(_Tab(key, label))
        if activate:
            self._active = key
        self._relayout()

    def remove(self, key: str) -> str | None:
        """Remove a tab, returning the key that should become active."""
        index = next((i for i, t in enumerate(self._tabs) if t.key == key), None)
        if index is None:
            return self._active

        self._tabs.pop(index)
        if self._active == key:
            # Prefer the tab that took its place, else the one before it --
            # the same choice a browser makes, and the least surprising.
            if self._tabs:
                self._active = self._tabs[min(index, len(self._tabs) - 1)].key
            else:
                self._active = None
        self._relayout()
        return self._active

    def find_tab(self, key: str) -> _Tab | None:
        return next((t for t in self._tabs if t.key == key), None)

    def set_label(self, key: str, label: str) -> None:
        tab = self.find_tab(key)
        if tab is not None and tab.label != label:
            tab.label = label
            self._relayout()

    def set_status(self, key: str, status: str) -> None:
        """Set a tab's indicator.

        This is how a background file announces that it changed while the user
        is looking at a different one.
        """
        tab = self.find_tab(key)
        if tab is not None and tab.status != status:
            tab.status = status
            self._redraw()

    def set_active(self, key: str | None) -> None:
        if key != self._active:
            self._active = key
            # Looking at a tab clears its "something happened" marker.
            tab = self.find_tab(key) if key else None
            if tab is not None and tab.status == TabStatus.CHANGED:
                tab.status = TabStatus.OK
            self._redraw()

    @property
    def active(self) -> str | None:
        return self._active

    @property
    def keys(self) -> list[str]:
        return [t.key for t in self._tabs]

    def __len__(self) -> int:
        return len(self._tabs)

    def select_relative(self, delta: int) -> str | None:
        """Move to the next or previous tab, wrapping around."""
        if not self._tabs:
            return None
        index = next((i for i, t in enumerate(self._tabs) if t.key == self._active), 0)
        target = self._tabs[(index + delta) % len(self._tabs)]
        self.set_active(target.key)
        if self._on_select is not None:
            self._on_select(target.key)
        return target.key

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def set_side(self, side: str) -> None:
        self._side = side
        self._relayout()

    def set_orientation(self, orientation: str) -> None:
        self._orientation = orientation
        self._relayout()

    @property
    def side(self) -> str:
        return self._side

    @property
    def orientation(self) -> str:
        return self._orientation

    @property
    def _stacked(self) -> bool:
        """True when tabs run down the strip rather than across it."""
        return self._orientation == Orientation.VERTICAL

    # ------------------------------------------------------------------
    # Layout and drawing
    # ------------------------------------------------------------------

    def _tab_extent(self, tab: _Tab) -> int:
        """Length of a tab along the direction the strip flows."""
        text = self._font.measure(tab.label)
        extra = _DOT + _GAP if tab.status != TabStatus.OK else 0
        return text + extra + _CLOSE + _PAD_X * 2

    def _thickness(self) -> int:
        """Depth of the strip across its flow direction."""
        return self._font.metrics("linespace") + _PAD_Y * 2

    def _relayout(self) -> None:
        """Position every tab and size the strip to fit."""
        thickness = self._thickness()
        offset = 0
        for tab in self._tabs:
            extent = self._tab_extent(tab)
            if self._stacked:
                tab.x, tab.y = 0, offset
                tab.width, tab.height = thickness, extent
            else:
                tab.x, tab.y = offset, 0
                tab.width, tab.height = extent, thickness
            offset += extent + 1

        # The strip requests only the depth it needs; the flow direction is
        # filled by the geometry manager.
        if self._stacked:
            self.configure(width=thickness)
        else:
            self.configure(height=thickness)
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        if not self._tabs:
            return

        for tab in self._tabs:
            self._draw_tab(tab, active=tab.key == self._active,
                           hover=tab.key == self._hover)

        # A line along the content edge joins the active tab to the view.
        width, height = self.winfo_width(), self.winfo_height()
        if self._side == Side.TOP:
            self.create_line(0, height - 1, width, height - 1, fill="#A0A0A0")
        elif self._side == Side.BOTTOM:
            self.create_line(0, 0, width, 0, fill="#A0A0A0")
        elif self._side == Side.LEFT:
            self.create_line(width - 1, 0, width - 1, height, fill="#A0A0A0")
        else:
            self.create_line(0, 0, 0, height, fill="#A0A0A0")

    def _draw_tab(self, tab: _Tab, active: bool, hover: bool) -> None:
        x, y, w, h = tab.x, tab.y, tab.width, tab.height
        if active:
            fill, outline = "#FFFFFF", "#A0A0A0"
        elif hover:
            fill, outline = "#E8E8E8", "#C0C0C0"
        else:
            fill, outline = "#DCDCDC", "#C0C0C0"

        self.create_rectangle(x, y, x + w, y + h, fill=fill, outline=outline)

        # Rotate the label when the strip runs vertically, so a side-mounted
        # strip stays narrow rather than as wide as the longest filename.
        angle = 90 if self._stacked else 0
        colour = "#000000" if active else "#404040"

        if self._stacked:
            text_x = x + w // 2
            text_y = y + h - _PAD_X
            anchor = "s"
            dot_x, dot_y = x + w // 2, y + _PAD_X
            close_cx, close_cy = x + w // 2, y + h - _PAD_X
        else:
            text_x = x + _PAD_X
            text_y = y + h // 2
            anchor = "w"
            dot_x, dot_y = x + w - _PAD_X - _CLOSE // 2, y + h // 2
            close_cx, close_cy = x + w - _PAD_X, y + h // 2

        if tab.status != TabStatus.OK:
            dot = STATUS_COLOURS.get(tab.status)
            if dot:
                if self._stacked:
                    text_y -= _DOT + _GAP
                self.create_oval(dot_x - _DOT // 2, dot_y - _DOT // 2,
                                 dot_x + _DOT // 2, dot_y + _DOT // 2,
                                 fill=dot, outline="")
                if not self._stacked:
                    # Leave room so the dot never overlaps the close button.
                    dot_x -= _DOT
        self.create_text(text_x, text_y, text=tab.label, anchor=anchor,
                         font=self._font, fill=colour, angle=angle)

        # The close cross appears only where it is actionable, which keeps a
        # strip of many tabs from looking like a wall of crosses.
        if active or hover:
            r = 4
            self.create_line(close_cx - r, close_cy - r, close_cx + r, close_cy + r,
                             fill="#606060", width=2)
            self.create_line(close_cx + r, close_cy - r, close_cx - r, close_cy + r,
                             fill="#606060", width=2)
            tab.close_box = (close_cx - r - 3, close_cy - r - 3,
                             close_cx + r + 3, close_cy + r + 3)
        else:
            tab.close_box = (0, 0, 0, 0)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _tab_at(self, x: int, y: int) -> _Tab | None:
        for tab in self._tabs:
            if tab.x <= x < tab.x + tab.width and tab.y <= y < tab.y + tab.height:
                return tab
        return None

    def _on_click(self, event) -> None:
        tab = self._tab_at(event.x, event.y)
        if tab is None:
            return
        x0, y0, x1, y1 = tab.close_box
        if x0 <= event.x <= x1 and y0 <= event.y <= y1:
            if self._on_close is not None:
                self._on_close(tab.key)
            return
        self.set_active(tab.key)
        if self._on_select is not None:
            self._on_select(tab.key)

    def _on_middle_click(self, event) -> None:
        tab = self._tab_at(event.x, event.y)
        if tab is not None and self._on_close is not None:
            self._on_close(tab.key)

    def _on_motion(self, event) -> None:
        tab = self._tab_at(event.x, event.y)
        key = tab.key if tab else None
        if key != self._hover:
            self._hover = key
            self._redraw()

    def _on_leave(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self._redraw()
