"""Shared dialog scaffolding.

Tk has no modal dialog of its own; every dialog has to grab input, centre
itself, wire Return and Escape, and hand a result back.  Doing that once here
keeps the four dialogs that follow to their actual content.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, ttk
from typing import Any

__all__ = ["ModalDialog", "ColourButton", "centre_on"]


def centre_on(window: tk.Toplevel, parent: tk.Misc) -> None:
    """Centre ``window`` over ``parent``, kept on screen.

    A dialog centred on a parent near a screen edge would otherwise open
    partly off-screen, which on Windows can put its buttons out of reach.
    """
    window.update_idletasks()
    width, height = window.winfo_width(), window.winfo_height()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()

    x = px + max(0, (pw - width) // 2)
    y = py + max(0, (ph - height) // 3)
    x = max(0, min(x, window.winfo_screenwidth() - width))
    y = max(0, min(y, window.winfo_screenheight() - height))
    window.geometry(f"+{x}+{y}")


class ModalDialog(tk.Toplevel):
    """A modal dialog returning a result through :attr:`result`.

    Subclasses fill :meth:`body` and read their widgets in :meth:`apply`.
    """

    def __init__(self, parent: tk.Misc, title: str, resizable: bool = False) -> None:
        super().__init__(parent)
        self.withdraw()                 # avoid a flash at the wrong position
        self.transient(parent)
        self.title(title)
        self.resizable(resizable, resizable)
        self.result: Any = None
        self._parent = parent

        container = ttk.Frame(self, padding=10)
        container.pack(fill="both", expand=True)
        self.body(container)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        self.buttons(buttons)

        self.bind("<Return>", self._on_ok)
        self.bind("<Escape>", self._on_cancel)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        centre_on(self, parent)
        self.deiconify()
        self.grab_set()
        self.focus_set()

    # ------------------------------------------------------------------

    def body(self, master: ttk.Frame) -> None:
        """Build the dialog's contents. Overridden by subclasses."""

    def buttons(self, master: ttk.Frame) -> None:
        ttk.Button(master, text="Cancel", command=self._on_cancel,
                   width=10).pack(side="right")
        ttk.Button(master, text="OK", command=self._on_ok,
                   width=10).pack(side="right", padx=(0, 6))

    def validate(self) -> bool:
        return True

    def apply(self) -> None:
        """Collect the result. Overridden by subclasses."""

    # ------------------------------------------------------------------

    def _on_ok(self, event=None) -> None:
        if not self.validate():
            return
        self.apply()
        self._close()

    def _on_cancel(self, event=None) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        # Returning focus before destroying avoids the parent losing
        # activation to another application on Windows.
        self._parent.focus_set()
        self.grab_release()
        self.destroy()

    def show(self) -> Any:
        """Run the dialog modally and return its result."""
        self.wait_window(self)
        return self.result


class ColourButton(tk.Button):
    """A button that shows a colour and opens a picker when clicked."""

    def __init__(self, master, colour: str = "#FFFFFF", label: str = "",
                 on_change=None, width: int = 4) -> None:
        super().__init__(master, width=width, relief="groove", borderwidth=2,
                         command=self._choose)
        self._colour = colour
        self._label = label
        self._on_change = on_change
        self._refresh()

    def _refresh(self) -> None:
        self.configure(background=self._colour, activebackground=self._colour)

    @property
    def colour(self) -> str:
        return self._colour

    @colour.setter
    def colour(self, value: str) -> None:
        self._colour = value
        self._refresh()

    def _choose(self) -> None:
        chosen = colorchooser.askcolor(color=self._colour, parent=self,
                                       title=self._label or "Choose colour")
        if chosen and chosen[1]:
            self._colour = chosen[1].upper()
            self._refresh()
            if self._on_change is not None:
                self._on_change(self._colour)
