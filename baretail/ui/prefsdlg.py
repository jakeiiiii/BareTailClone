"""Preferences and font selection.

Both dialogs edit the :class:`~..config.Config` dictionary directly on OK, so
there is no second copy of the settings schema to keep in step.

The font dialog is separate from Preferences because BareTail puts it on the
View menu, where it is reached far more often than the rest of the settings --
and because it needs a live preview that the tabbed dialog has no room for.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from ..config import StorageMode
from ..core import encoding as enc
from .dialogs import ColourButton, ModalDialog
from .tkutil import release_font, view_font
from .tabstrip import Orientation, Side

__all__ = ["PreferencesDialog", "FontDialog"]

_PREVIEW = ("2024-01-01 12:00:00 [INFO ] engine\t#00000042 started\n"
            "2024-01-01 12:00:01 [ERROR] db.session\t#00000043 timeout")


class FontDialog(ModalDialog):
    """Chooses the view font, with the spacing and offset BareTail exposes.

    Only fixed-pitch families are offered: a log viewer aligns columns by
    character position, and a proportional font destroys that.
    """

    def __init__(self, parent, family: str, size: int,
                 spacing: int = 0, offset: int = 0) -> None:
        self._initial = (family, size, spacing, offset)
        super().__init__(parent, "Font")

    def body(self, master: ttk.Frame) -> None:
        family, size, spacing, offset = self._initial

        ttk.Label(master, text="Font:").grid(row=0, column=0, sticky="w")
        self._family = tk.StringVar(value=family)
        families = sorted({f for f in tkfont.families()
                           if not f.startswith("@") and _is_fixed(f)})
        if family not in families:
            families.insert(0, family)
        self._family_box = ttk.Combobox(master, textvariable=self._family,
                                        values=families, state="readonly", width=28)
        self._family_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Label(master, text="Size:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._size = tk.IntVar(value=size)
        ttk.Spinbox(master, from_=6, to=48, textvariable=self._size, width=6,
                    command=self._preview).grid(row=1, column=1, sticky="w",
                                                padx=(6, 0), pady=(6, 0))

        ttk.Label(master, text="Line spacing:").grid(row=2, column=0, sticky="w",
                                                     pady=(6, 0))
        self._spacing = tk.IntVar(value=spacing)
        ttk.Spinbox(master, from_=0, to=20, textvariable=self._spacing, width=6,
                    command=self._preview).grid(row=2, column=1, sticky="w",
                                                padx=(6, 0), pady=(6, 0))

        ttk.Label(master, text="Line offset:").grid(row=3, column=0, sticky="w",
                                                    pady=(6, 0))
        self._offset = tk.IntVar(value=offset)
        ttk.Spinbox(master, from_=0, to=20, textvariable=self._offset, width=6,
                    command=self._preview).grid(row=3, column=1, sticky="w",
                                                padx=(6, 0), pady=(6, 0))

        self._preview_font = view_font(family=family, size=size)
        self._preview_text = tk.Text(master, height=3, width=54, wrap="none",
                                     font=self._preview_font, state="disabled",
                                     relief="sunken", borderwidth=1)
        self._preview_text.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self._family_box.bind("<<ComboboxSelected>>", lambda e: self._preview())
        for var in (self._size, self._spacing, self._offset):
            var.trace_add("write", lambda *a: self._preview())
        self._preview()

    def _preview(self) -> None:
        try:
            size = max(6, min(48, int(self._size.get())))
            spacing = max(0, int(self._spacing.get()))
            offset = max(0, int(self._offset.get()))
        except (tk.TclError, ValueError):
            # Mid-edit the spinbox can be empty or partial; keep the last
            # good preview rather than raising on every keystroke.
            return
        self._preview_font.configure(family=self._family.get(), size=size)
        self._preview_text.configure(state="normal", spacing1=offset, spacing3=spacing)
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("1.0", _PREVIEW)
        self._preview_text.configure(state="disabled")

    def apply(self) -> None:
        try:
            self.result = {
                "family": self._family.get(),
                "size": max(6, min(48, int(self._size.get()))),
                "spacing": max(0, int(self._spacing.get())),
                "offset": max(0, int(self._offset.get())),
            }
        except (tk.TclError, ValueError):
            self.result = None


def _is_fixed(family: str) -> bool:
    """Whether a family is fixed-pitch.

    Measured rather than guessed from the name, since the useful monospace
    families on a given machine are not a fixed list.
    """
    font = None
    try:
        # One throwaway font per installed family, so these are the most
        # likely of all to be collected on a worker thread; view_font makes
        # that harmless, and the release below keeps Tcl tidy anyway.
        font = view_font(family=family, size=10)
        return font.measure("i") == font.measure("W")
    except tk.TclError:
        return False
    finally:
        release_font(font)


class PreferencesDialog(ModalDialog):
    """Edits everything that is not a highlight rule or a font."""

    def __init__(self, parent, config) -> None:
        self._config = config
        super().__init__(parent, "Preferences")

    def body(self, master: ttk.Frame) -> None:
        book = ttk.Notebook(master)
        book.pack(fill="both", expand=True)
        book.add(self._view_page(book), text="View")
        book.add(self._files_page(book), text="Files")
        book.add(self._tabs_page(book), text="Tabs")
        book.add(self._storage_page(book), text="Storage")

    # ------------------------------------------------------------------

    def _view_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=10)
        get = self._config.get

        self._wrap = tk.BooleanVar(value=get("view.wrap", False))
        ttk.Checkbutton(page, text="Wrap long lines",
                        variable=self._wrap).grid(row=0, column=0, columnspan=2,
                                                  sticky="w")

        self._follow_on_open = tk.BooleanVar(value=get("view.follow", True))
        ttk.Checkbutton(page, text="Follow tail when opening a file",
                        variable=self._follow_on_open).grid(row=1, column=0,
                                                            columnspan=2, sticky="w")

        ttk.Label(page, text="Tab width:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._tab_width = tk.IntVar(value=get("view.tab_width", 8))
        ttk.Spinbox(page, from_=1, to=32, textvariable=self._tab_width,
                    width=6).grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(8, 0))

        colours = ttk.LabelFrame(page, text="Default colours", padding=8)
        colours.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(colours, text="Text:").pack(side="left")
        self._fg = ColourButton(colours, get("colours.fg", "#000000"), "Text colour")
        self._fg.pack(side="left", padx=(4, 12))
        ttk.Label(colours, text="Background:").pack(side="left")
        self._bg = ColourButton(colours, get("colours.bg", "#FFFFFF"), "Background")
        self._bg.pack(side="left", padx=(4, 0))
        return page

    def _files_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=10)
        get = self._config.get

        ttk.Label(page, text="Default character set:").grid(row=0, column=0, sticky="w")
        self._encoding = tk.StringVar(value=get("encoding.default", "ANSI"))
        ttk.Combobox(page, textvariable=self._encoding, state="readonly", width=14,
                     values=[c.label for c in enc.CODECS]).grid(
                         row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(page, foreground="#606060",
                  text="Used when a file has no byte-order mark and its\n"
                       "contents are not recognisable as UTF-8 or UTF-16.").grid(
                           row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

        ttk.Label(page, text="Check for changes every:").grid(row=2, column=0, sticky="w")
        self._poll = tk.DoubleVar(value=get("poll_interval", 0.25))
        ttk.Spinbox(page, from_=0.05, to=10.0, increment=0.05, format="%.2f",
                    textvariable=self._poll, width=8).grid(row=2, column=1, sticky="w",
                                                           padx=(6, 0))
        ttk.Label(page, text="seconds").grid(row=2, column=2, sticky="w", padx=(4, 0))
        return page

    def _tabs_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=10)
        get = self._config.get

        ttk.Label(page, text="Position:").grid(row=0, column=0, sticky="w")
        self._side = tk.StringVar(value=get("tabs.side", Side.TOP))
        row = ttk.Frame(page)
        row.grid(row=0, column=1, sticky="w", padx=(6, 0))
        for label, value in (("Top", Side.TOP), ("Bottom", Side.BOTTOM),
                             ("Left", Side.LEFT), ("Right", Side.RIGHT)):
            ttk.Radiobutton(row, text=label, value=value,
                            variable=self._side).pack(side="left", padx=(0, 8))

        ttk.Label(page, text="Orientation:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._orientation = tk.StringVar(
            value=get("tabs.orientation", Orientation.HORIZONTAL))
        row = ttk.Frame(page)
        row.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        for label, value in (("Horizontal", Orientation.HORIZONTAL),
                             ("Vertical", Orientation.VERTICAL)):
            ttk.Radiobutton(row, text=label, value=value,
                            variable=self._orientation).pack(side="left", padx=(0, 8))

        self._tabs_visible = tk.BooleanVar(value=get("tabs.visible", True))
        ttk.Checkbutton(page, text="Show tabs even with a single file",
                        variable=self._tabs_visible).grid(row=2, column=0, columnspan=2,
                                                          sticky="w", pady=(10, 0))
        return page

    def _storage_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=10)
        self._storage = tk.StringVar(value=self._config.storage)
        ttk.Label(page, text="Keep preferences:").pack(anchor="w")
        for label, value in (
            ("In a file beside the application (portable)", StorageMode.FILE),
            ("In the registry", StorageMode.REGISTRY),
            ("Do not save preferences", StorageMode.NONE),
        ):
            ttk.Radiobutton(page, text=label, value=value,
                            variable=self._storage).pack(anchor="w", pady=(4, 0))
        ttk.Label(page, foreground="#606060", wraplength=380, justify="left",
                  text=f"Settings file: {self._config.path}").pack(anchor="w",
                                                                   pady=(10, 0))
        return page

    # ------------------------------------------------------------------

    def apply(self) -> None:
        config = self._config
        try:
            tab_width = max(1, min(32, int(self._tab_width.get())))
            poll = max(0.05, min(10.0, float(self._poll.get())))
        except (tk.TclError, ValueError):
            tab_width = config.get("view.tab_width", 8)
            poll = config.get("poll_interval", 0.25)

        config.set("view.wrap", self._wrap.get())
        config.set("view.follow", self._follow_on_open.get())
        config.set("view.tab_width", tab_width)
        config.set("colours.fg", self._fg.colour)
        config.set("colours.bg", self._bg.colour)
        config.set("encoding.default", self._encoding.get())
        config.set("poll_interval", poll)
        config.set("tabs.side", self._side.get())
        config.set("tabs.orientation", self._orientation.get())
        config.set("tabs.visible", self._tabs_visible.get())
        config.storage = self._storage.get()
        self.result = config
