"""The highlight rule editor.

Rules are resolved first-match-wins, so their **order is part of the
configuration**, not a display preference.  The list is therefore explicitly
ordered with Move Up / Move Down rather than being sorted, and each row is
drawn in its own colours so the effect is visible while editing rather than
only after closing the dialog.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..core.highlight import DEFAULT_RULES, HighlightRule, RuleSet
from .dialogs import ColourButton, ModalDialog

__all__ = ["HighlightDialog"]

_SAMPLE = "The quick brown fox — 2024-01-01 ERROR sample text"


class HighlightDialog(ModalDialog):
    """Edits a :class:`RuleSet`.

    Works on a copy; :attr:`result` is the edited set, or None if cancelled,
    so abandoning the dialog leaves the live rules untouched.
    """

    def __init__(self, parent, ruleset: RuleSet) -> None:
        self._rules = ruleset.copy()
        self._selected: int | None = None
        self._loading = False
        super().__init__(parent, "Highlighting", resizable=True)
        self.minsize(560, 420)

    # ------------------------------------------------------------------

    def body(self, master: ttk.Frame) -> None:
        master.columnconfigure(0, weight=1)
        master.rowconfigure(1, weight=1)

        self._enabled = tk.BooleanVar(value=self._rules.enabled)
        ttk.Checkbutton(master, text="Enable highlighting",
                        variable=self._enabled).grid(row=0, column=0, sticky="w",
                                                     pady=(0, 6))

        self._build_list(master)
        self._build_editor(master)

        self._refresh_list()
        if len(self._rules):
            self._select(0)

    # ------------------------------------------------------------------

    def _build_list(self, master: ttk.Frame) -> None:
        frame = ttk.Frame(master)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(frame, columns=("on", "pattern", "type"),
                                  show="headings", selectmode="browse", height=8)
        self._tree.heading("on", text="")
        self._tree.heading("pattern", text="Text")
        self._tree.heading("type", text="Match")
        self._tree.column("on", width=28, anchor="center", stretch=False)
        self._tree.column("pattern", width=320)
        self._tree.column("type", width=110, stretch=False)
        self._tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>", self._on_toggle_enabled)

        side = ttk.Frame(frame)
        side.grid(row=0, column=2, sticky="ns", padx=(6, 0))
        for text, command in (("Add", self._add), ("Remove", self._remove),
                              ("Move Up", lambda: self._move(-1)),
                              ("Move Down", lambda: self._move(1))):
            ttk.Button(side, text=text, width=11, command=command).pack(pady=2)
        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=8)
        ttk.Button(side, text="Defaults", width=11, command=self._defaults).pack(pady=2)

        # Order decides which of two overlapping rules applies, so say so
        # rather than leaving the user to discover it.
        ttk.Label(master, foreground="#606060",
                  text="Rules are applied top to bottom; the first match wins.").grid(
                      row=2, column=0, sticky="w", pady=(6, 0))

    def _build_editor(self, master: ttk.Frame) -> None:
        box = ttk.LabelFrame(master, text="Selected rule", padding=8)
        box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Text:").grid(row=0, column=0, sticky="w")
        self._pattern = tk.StringVar()
        self._pattern.trace_add("write", lambda *a: self._on_edit())
        self._pattern_entry = ttk.Entry(box, textvariable=self._pattern)
        self._pattern_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 0))

        self._error = ttk.Label(box, foreground="#CC3333", text="")
        self._error.grid(row=1, column=1, columnspan=3, sticky="w", padx=(6, 0))

        options = ttk.Frame(box)
        options.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._regex = tk.BooleanVar()
        self._case = tk.BooleanVar()
        self._rule_enabled = tk.BooleanVar()
        ttk.Checkbutton(options, text="Enabled", variable=self._rule_enabled,
                        command=self._on_edit).pack(side="left")
        ttk.Checkbutton(options, text="Regular expression", variable=self._regex,
                        command=self._on_edit).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(options, text="Match case", variable=self._case,
                        command=self._on_edit).pack(side="left", padx=(12, 0))

        colours = ttk.Frame(box)
        colours.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(colours, text="Text:").pack(side="left")
        self._fg = ColourButton(colours, "#000000", "Text colour",
                                on_change=lambda c: self._on_edit())
        self._fg.pack(side="left", padx=(4, 12))
        ttk.Label(colours, text="Background:").pack(side="left")
        self._bg = ColourButton(colours, "#FFFF80", "Background colour",
                                on_change=lambda c: self._on_edit())
        self._bg.pack(side="left", padx=(4, 12))

        # A live sample makes a colour pair's readability obvious before the
        # dialog is closed and applied to a whole file.
        self._sample = tk.Label(box, text=_SAMPLE, anchor="w", padx=6, pady=3,
                                relief="sunken", borderwidth=1)
        self._sample.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------
    # List handling
    # ------------------------------------------------------------------

    def _refresh_list(self, keep: int | None = None) -> None:
        selection = keep if keep is not None else self._selected
        self._tree.delete(*self._tree.get_children())
        for index, rule in enumerate(self._rules):
            kind = "Regex" if rule.is_regex else "Text"
            if rule.case_sensitive:
                kind += ", case"
            label = rule.pattern or "(empty)"
            if not rule.valid:
                label += "  ⚠"
            self._tree.insert("", "end", iid=str(index),
                              values=("✓" if rule.enabled else "", label, kind),
                              tags=(f"row{index}",))
            # Show each rule in the colours it will actually paint with.
            self._tree.tag_configure(f"row{index}", background=rule.bg,
                                     foreground=rule.fg)
        if selection is not None and 0 <= selection < len(self._rules):
            self._tree.selection_set(str(selection))
            self._tree.focus(str(selection))

    def _select(self, index: int) -> None:
        self._tree.selection_set(str(index))
        self._tree.focus(str(index))
        self._load(index)

    def _on_select(self, event=None) -> None:
        selection = self._tree.selection()
        if selection:
            self._load(int(selection[0]))

    def _load(self, index: int) -> None:
        """Copy a rule into the editor fields."""
        if not (0 <= index < len(self._rules)):
            return
        self._selected = index
        rule = self._rules[index]
        # Suppress the trace callbacks that populating the fields would fire,
        # which would otherwise write the previous rule's values into this one.
        self._loading = True
        try:
            self._pattern.set(rule.pattern)
            self._regex.set(rule.is_regex)
            self._case.set(rule.case_sensitive)
            self._rule_enabled.set(rule.enabled)
            self._fg.colour = rule.fg
            self._bg.colour = rule.bg
        finally:
            self._loading = False
        self._update_sample(rule)

    def _on_edit(self) -> None:
        """Write the editor fields back to the selected rule."""
        if self._loading or self._selected is None:
            return
        if not (0 <= self._selected < len(self._rules)):
            return
        rule = self._rules[self._selected]
        rule.pattern = self._pattern.get()
        rule.is_regex = self._regex.get()
        rule.case_sensitive = self._case.get()
        rule.enabled = self._rule_enabled.get()
        rule.fg = self._fg.colour
        rule.bg = self._bg.colour
        self._update_sample(rule)
        self._refresh_list(keep=self._selected)

    def _update_sample(self, rule: HighlightRule) -> None:
        self._sample.configure(background=rule.bg, foreground=rule.fg)
        self._error.configure(text=rule.error or "")

    def _on_toggle_enabled(self, event=None) -> str:
        selection = self._tree.selection()
        if selection:
            index = int(selection[0])
            self._rules[index].enabled = not self._rules[index].enabled
            self._load(index)
            self._refresh_list(keep=index)
        return "break"

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _add(self) -> None:
        self._rules.rules.append(HighlightRule(pattern=""))
        index = len(self._rules) - 1
        self._refresh_list(keep=index)
        self._select(index)
        self._pattern_entry.focus_set()

    def _remove(self) -> None:
        if self._selected is None or not len(self._rules):
            return
        index = self._selected
        self._rules.rules.pop(index)
        self._selected = None
        self._refresh_list()
        if len(self._rules):
            self._select(min(index, len(self._rules) - 1))
        else:
            self._clear_editor()

    def _move(self, delta: int) -> None:
        if self._selected is None:
            return
        new_index = self._rules.move(self._selected, delta)
        self._selected = new_index
        self._refresh_list(keep=new_index)
        self._select(new_index)

    def _defaults(self) -> None:
        self._rules = RuleSet([rule.copy() for rule in DEFAULT_RULES],
                              enabled=self._enabled.get())
        self._selected = None
        self._refresh_list()
        if len(self._rules):
            self._select(0)

    def _clear_editor(self) -> None:
        self._loading = True
        try:
            self._pattern.set("")
            self._regex.set(False)
            self._case.set(False)
            self._rule_enabled.set(True)
        finally:
            self._loading = False
        self._error.configure(text="")

    # ------------------------------------------------------------------

    def apply(self) -> None:
        self._rules.enabled = self._enabled.get()
        # An empty pattern would match every line, colouring the whole file.
        self._rules.rules = [r for r in self._rules.rules if r.pattern]
        self.result = self._rules
