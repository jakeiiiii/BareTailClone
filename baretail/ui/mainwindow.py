"""The application window.

Holds the menus, toolbar, tab strip, status bar, and one view per open file.

Each open file owns a :class:`FileTab`: the :class:`LineFile` that reads it, an
:class:`Indexer` counting its lines in the background, a :class:`Tailer`
watching it for changes, and the viewport showing it.  Those three threads
never touch a widget; everything they report is posted through the
:class:`~.pump.UiPump` and applied on the main thread.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import config as cfg
from ..core import encoding as enc
from ..core.highlight import RuleSet
from ..core.indexer import Indexer
from ..core.linefile import LineFile
from ..core.search import FilterMode, Matcher, SearchEngine
from ..core.tailer import ChangeKind, Tailer
from .dialogs import centre_on
from .filterview import FilteredView
from .highlightdlg import HighlightDialog
from .logview import VirtualTextView
from .prefsdlg import FontDialog, PreferencesDialog
from .pump import UiPump
from .searchbar import SearchPanel
from .tabstrip import Orientation, Side, TabStatus, TabStrip

__all__ = ["MainWindow", "FileTab"]

APP_NAME = "BareTail"

#: Where the tab strip is packed for each side setting.
_SIDE_TO_PACK = {Side.TOP: "top", Side.BOTTOM: "bottom",
                 Side.LEFT: "left", Side.RIGHT: "right"}


class FileTab:
    """Everything belonging to one open file."""

    def __init__(self, key: str, path: str, linefile: LineFile,
                 view: VirtualTextView) -> None:
        self.key = key
        self.path = path
        self.linefile = linefile
        self.view = view
        self.indexer: Indexer | None = None
        self.tailer: Tailer | None = None

        #: Filter state, when filtering is on for this file.
        self.filter_view: FilteredView | None = None
        self.filter_matcher: Matcher | None = None
        self.filter_mode: str = FilterMode.INCLUDE
        self.filter_engine: SearchEngine | None = None
        self.filter_scanned_to: int = 0

        self.error: str | None = None

    @property
    def label(self) -> str:
        return os.path.basename(self.path) or self.path

    @property
    def filtering(self) -> bool:
        return self.filter_view is not None

    def shutdown(self) -> None:
        for worker in (self.indexer, self.tailer, self.filter_engine):
            if worker is not None:
                try:
                    worker.stop() if hasattr(worker, "stop") else worker.cancel()
                except Exception:
                    pass
        self.linefile.close()


class MainWindow(tk.Tk):
    """The BareTail main window."""

    def __init__(self, config: cfg.Config | None = None) -> None:
        super().__init__()
        self.config_data = config or cfg.Config.load()
        self.title(APP_NAME)

        self._tabs: dict[str, FileTab] = {}
        self._next_key = 0
        self._rules: RuleSet = self.config_data.ruleset()
        self._search_visible = False

        self._pump = UiPump(self)
        self._pump.start()

        self._build_ui()
        self._apply_config()
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.config_data.load_error:
            self._set_status(f"Settings not loaded — {self.config_data.load_error}")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_menu()

        self._toolbar = ttk.Frame(self, padding=(4, 3))
        self._toolbar.pack(side="top", fill="x")
        self._build_toolbar()
        ttk.Separator(self, orient="horizontal").pack(side="top", fill="x")

        self._status_bar = ttk.Frame(self, padding=(6, 2))
        self._status_bar.pack(side="bottom", fill="x")
        self._build_status_bar()

        # The search panel sits between the content and the status bar, and is
        # created lazily the first time it is asked for.
        self._search_holder = ttk.Frame(self)
        self._search: SearchPanel | None = None

        self._body = ttk.Frame(self)
        self._body.pack(side="top", fill="both", expand=True)

        self._tabstrip = TabStrip(self._body, on_select=self._on_tab_selected,
                                  on_close=self.close_tab)
        self._content = ttk.Frame(self._body)
        self._place_tabstrip()

        self._placeholder = ttk.Label(
            self._content, anchor="center", foreground="#808080",
            text="No file open.\n\nUse File ▸ Open, or drag a log file onto the window.")
        self._placeholder.pack(fill="both", expand=True)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_dialog)
        file_menu.add_command(label="Close Tab", accelerator="Ctrl+W",
                              command=lambda: self.close_tab(self._tabstrip.active))
        file_menu.add_separator()
        self._recent_menu = tk.Menu(file_menu, tearoff=0)
        file_menu.add_cascade(label="Recent Files", menu=self._recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Save Session…", command=self.save_session)
        file_menu.add_command(label="Load Session…", command=self.load_session)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Copy", accelerator="Ctrl+C", command=self._copy)
        edit_menu.add_command(label="Select All", accelerator="Ctrl+A",
                              command=self._select_all)
        edit_menu.add_command(label="Copy Whole File", command=self._copy_all)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        self._follow_var = tk.BooleanVar(value=True)
        view_menu.add_checkbutton(label="Follow Tail", accelerator="F12",
                                  variable=self._follow_var, command=self._toggle_follow)
        self._wrap_var = tk.BooleanVar(value=False)
        view_menu.add_checkbutton(label="Word Wrap", variable=self._wrap_var,
                                  command=self._toggle_wrap)
        self._highlight_var = tk.BooleanVar(value=self._rules.enabled)
        view_menu.add_checkbutton(label="Highlighting", accelerator="Ctrl+H",
                                  variable=self._highlight_var,
                                  command=self._toggle_highlighting)
        view_menu.add_separator()
        view_menu.add_command(label="Highlights…", command=self.edit_highlights)
        view_menu.add_command(label="Font…", command=self.choose_font)
        view_menu.add_separator()

        tabs_menu = tk.Menu(view_menu, tearoff=0)
        self._side_var = tk.StringVar(value=Side.TOP)
        for label, value in (("Top", Side.TOP), ("Bottom", Side.BOTTOM),
                             ("Left", Side.LEFT), ("Right", Side.RIGHT)):
            tabs_menu.add_radiobutton(label=label, value=value, variable=self._side_var,
                                      command=self._apply_tab_placement)
        tabs_menu.add_separator()
        self._orientation_var = tk.StringVar(value=Orientation.HORIZONTAL)
        for label, value in (("Horizontal", Orientation.HORIZONTAL),
                             ("Vertical", Orientation.VERTICAL)):
            tabs_menu.add_radiobutton(label=label, value=value,
                                      variable=self._orientation_var,
                                      command=self._apply_tab_placement)
        view_menu.add_cascade(label="Tabs", menu=tabs_menu)
        view_menu.add_separator()
        view_menu.add_command(label="Go to Start", accelerator="Ctrl+Home",
                              command=lambda: self._with_view(lambda v: v.goto_start()))
        view_menu.add_command(label="Go to End", accelerator="Ctrl+End",
                              command=lambda: self._with_view(lambda v: v.goto_end()))
        view_menu.add_command(label="Go to Line…", accelerator="Ctrl+G",
                              command=self.goto_line)
        menubar.add_cascade(label="View", menu=view_menu)

        search_menu = tk.Menu(menubar, tearoff=0)
        search_menu.add_command(label="Find…", accelerator="Ctrl+F",
                                command=self.show_search)
        search_menu.add_command(label="Find Next", accelerator="F3",
                                command=lambda: self._find(False))
        search_menu.add_command(label="Find Previous", accelerator="Shift+F3",
                                command=lambda: self._find(True))
        search_menu.add_separator()
        search_menu.add_command(label="Clear Filter", command=self.clear_filter)
        menubar.add_cascade(label="Search", menu=search_menu)

        prefs_menu = tk.Menu(menubar, tearoff=0)
        prefs_menu.add_command(label="Preferences…", command=self.edit_preferences)
        prefs_menu.add_separator()
        prefs_menu.add_command(label="Load from File…", command=self.load_prefs_file)
        prefs_menu.add_command(label="Save to File…", command=self.save_prefs_file)
        prefs_menu.add_separator()
        self._storage_var = tk.StringVar(value=self.config_data.storage)
        for label, value in (("Store in File", cfg.StorageMode.FILE),
                             ("Store in Registry", cfg.StorageMode.REGISTRY),
                             ("Do Not Store", cfg.StorageMode.NONE)):
            prefs_menu.add_radiobutton(label=label, value=value,
                                       variable=self._storage_var,
                                       command=self._set_storage)
        menubar.add_cascade(label="Preferences", menu=prefs_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=menubar)
        self._refresh_recent_menu()

    def _build_toolbar(self) -> None:
        """Build the toolbar.

        Word labels rather than emoji icons.  Emoji render inconsistently in
        ttk buttons -- the folder glyph came out as an empty box on Windows,
        giving the Open button no visible label at all -- and a monochrome
        pictogram is not clearer than the word it stands for.
        """
        def button(text, command, tooltip):
            widget = ttk.Button(self._toolbar, text=text, width=len(text) + 2,
                                command=command)
            widget.pack(side="left", padx=1)
            _Tooltip(widget, tooltip)
            return widget

        def toggle(text, variable, command, tooltip):
            widget = ttk.Checkbutton(self._toolbar, text=text, style="Toolbutton",
                                     variable=variable, command=command)
            widget.pack(side="left", padx=1)
            _Tooltip(widget, tooltip)
            return widget

        def separator():
            ttk.Separator(self._toolbar, orient="vertical").pack(
                side="left", fill="y", padx=5, pady=2)

        button("Open", self.open_dialog, "Open a file  (Ctrl+O)")
        separator()

        self._follow_button = toggle(
            "Follow", self._follow_var, self._toggle_follow,
            "Follow the end of the file as it grows  (F12)")
        self._highlight_button = toggle(
            "Highlight", self._highlight_var, self._toggle_highlighting,
            "Turn highlighting on or off  (Ctrl+H)")
        self._wrap_button = toggle(
            "Wrap", self._wrap_var, self._toggle_wrap, "Wrap long lines")

        separator()
        button("Find", self.show_search, "Find  (Ctrl+F)")
        button("Rules", self.edit_highlights, "Edit highlight rules")

    def _build_status_bar(self) -> None:
        self._status_text = ttk.Label(self._status_bar, text="Ready", anchor="w")
        self._status_text.pack(side="left", fill="x", expand=True)
        for attribute in ("_status_pos", "_status_lines", "_status_size",
                          "_status_encoding"):
            label = ttk.Label(self._status_bar, text="", anchor="e", width=18)
            label.pack(side="right", padx=(8, 0))
            setattr(self, attribute, label)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _apply_config(self) -> None:
        get = self.config_data.get

        width = int(get("window.width", 1000))
        height = int(get("window.height", 700))
        x, y = get("window.x"), get("window.y")
        if x is not None and y is not None:
            # Only restore an on-screen position: a window saved on a monitor
            # that is no longer attached would open where nobody can reach it.
            if -50 <= int(x) <= self.winfo_screenwidth() - 100 and \
                    -50 <= int(y) <= self.winfo_screenheight() - 100:
                self.geometry(f"{width}x{height}+{int(x)}+{int(y)}")
            else:
                self.geometry(f"{width}x{height}")
        else:
            self.geometry(f"{width}x{height}")
        if int(get("window.state", 0)) == 2:
            self.state("zoomed")

        self._wrap_var.set(bool(get("view.wrap", False)))
        self._follow_var.set(bool(get("view.follow", True)))
        self._highlight_var.set(bool(get("view.highlights_enabled", True)))
        self._side_var.set(get("tabs.side", Side.TOP))
        self._orientation_var.set(get("tabs.orientation", Orientation.HORIZONTAL))
        self._storage_var.set(self.config_data.storage)
        self._apply_tab_placement()

    def _collect_config(self) -> None:
        """Write current UI state back into the settings."""
        set_ = self.config_data.set
        state = self.state()
        set_("window.state", 2 if state == "zoomed" else 0)
        if state != "zoomed":
            set_("window.width", self.winfo_width())
            set_("window.height", self.winfo_height())
            set_("window.x", self.winfo_x())
            set_("window.y", self.winfo_y())
        set_("view.wrap", self._wrap_var.get())
        set_("view.follow", self._follow_var.get())
        set_("tabs.side", self._side_var.get())
        set_("tabs.orientation", self._orientation_var.get())
        self._rules.enabled = self._highlight_var.get()
        self.config_data.store_ruleset(self._rules)

    def _font_settings(self) -> tuple[str, int, int, int]:
        get = self.config_data.get
        return (get("font.family", "Consolas"), int(get("font.size", 10)),
                int(get("font.spacing", 0)), int(get("font.offset", 0)))

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def _bind_keys(self) -> None:
        bindings = {
            "<Control-o>": lambda e: self.open_dialog(),
            "<Control-w>": lambda e: self.close_tab(self._tabstrip.active),
            "<Control-f>": lambda e: self.show_search(),
            "<Control-h>": lambda e: self._toggle_highlighting(toggle=True),
            "<Control-g>": lambda e: self.goto_line(),
            "<F3>": lambda e: self._find(False),
            "<Shift-F3>": lambda e: self._find(True),
            "<F12>": lambda e: self._toggle_follow(toggle=True),
            "<Control-Tab>": lambda e: self._tabstrip.select_relative(1),
            "<Control-Shift-Tab>": lambda e: self._tabstrip.select_relative(-1),
            "<Control-Prior>": lambda e: self._tabstrip.select_relative(-1),
            "<Control-Next>": lambda e: self._tabstrip.select_relative(1),
            "<Escape>": lambda e: self.hide_search(),
        }
        for sequence, handler in bindings.items():
            self.bind_all(sequence, handler)

    # ------------------------------------------------------------------
    # Opening and closing files
    # ------------------------------------------------------------------

    def open_dialog(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title="Open log file",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")])
        for path in paths:
            self.open_file(path)

    def open_file(self, path: str, activate: bool = True) -> FileTab | None:
        """Open a file in a new tab, or focus it if already open."""
        path = os.path.abspath(path)

        existing = next((t for t in self._tabs.values() if t.path == path), None)
        if existing is not None:
            self._tabstrip.set_active(existing.key)
            self._show_tab(existing.key)
            return existing

        default = enc.by_label(self.config_data.get("encoding.default", "ANSI"))
        try:
            linefile = LineFile(path, default_codec=default)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Cannot open {path}\n\n{exc}", parent=self)
            return None

        key = f"tab{self._next_key}"
        self._next_key += 1

        family, size, spacing, offset = self._font_settings()
        view = VirtualTextView(
            self._content, linefile, self._rules,
            font_family=family, font_size=size, wrap=self._wrap_var.get(),
            tab_width=int(self.config_data.get("view.tab_width", 8)),
            line_spacing=spacing, line_offset=offset,
            on_position_change=self._update_status)
        view.set_colours(self.config_data.get("colours.fg", "#000000"),
                         self.config_data.get("colours.bg", "#FFFFFF"))

        tab = FileTab(key, path, linefile, view)
        self._tabs[key] = tab

        tab.indexer = Indexer(
            linefile,
            on_progress=lambda done, total, lines, complete:
                self._pump.post(self._on_index_progress, key, lines, complete))
        tab.indexer.start()

        tab.tailer = Tailer(
            linefile,
            on_change=lambda kind, size: self._pump.post(self._on_file_change, key,
                                                         kind, size),
            interval=float(self.config_data.get("poll_interval", 0.25)))
        tab.tailer.start()

        # Position the new view *before* showing it.  _show_tab syncs the
        # follow checkbox from whichever view it raises -- right when switching
        # between files, each of which has its own follow state -- so doing it
        # first would overwrite the preference with the new view's default and
        # leave the file opening at the top.
        #
        # Opening at the end and following is what a log viewer is for; the
        # interesting lines are the newest ones.
        if self._follow_var.get():
            view.goto_end()
        else:
            view.goto_start()

        self._tabstrip.add(key, tab.label, activate=activate)
        self.config_data.add_recent(path)
        self._refresh_recent_menu()

        if activate:
            self._show_tab(key)
        return tab

    def close_tab(self, key: str | None) -> None:
        if key is None or key not in self._tabs:
            return
        tab = self._tabs.pop(key)
        tab.shutdown()
        tab.view.destroy()
        if tab.filter_view is not None:
            tab.filter_view.destroy()

        next_key = self._tabstrip.remove(key)
        self._show_tab(next_key)

    @property
    def active_tab(self) -> FileTab | None:
        key = self._tabstrip.active
        return self._tabs.get(key) if key else None

    def _on_tab_selected(self, key: str) -> None:
        self._show_tab(key)

    def _show_tab(self, key: str | None) -> None:
        """Raise one tab's view and hide the rest."""
        for other in self._tabs.values():
            other.view.pack_forget()
            if other.filter_view is not None:
                other.filter_view.pack_forget()

        tab = self._tabs.get(key) if key else None
        if tab is None:
            self._placeholder.pack(fill="both", expand=True)
            self._update_status()
            self.title(APP_NAME)
            return

        self._placeholder.pack_forget()
        widget = tab.filter_view if tab.filtering else tab.view
        widget.pack(fill="both", expand=True)
        widget.focus_set()
        self._tabstrip.set_active(key)
        self._follow_var.set(tab.view.following)
        self.title(f"{tab.label} — {APP_NAME}")
        self._update_status()

    # ------------------------------------------------------------------
    # Background events
    # ------------------------------------------------------------------

    def _on_index_progress(self, key: str, lines: int, complete: bool) -> None:
        if key in self._tabs and key == self._tabstrip.active:
            self._update_status()

    def _on_file_change(self, key: str, kind: str, size: int) -> None:
        """Apply a change reported by a tailer, on the main thread."""
        tab = self._tabs.get(key)
        if tab is None:
            return

        if kind in (ChangeKind.TRUNCATED, ChangeKind.ROTATED, ChangeKind.RESTORED):
            tab.view.on_file_reset()
            if tab.filter_view is not None:
                tab.filter_view.reset()
                tab.filter_scanned_to = tab.linefile.content_start
                self._scan_filter(tab)
            tab.error = None
            self._tabstrip.set_status(key, TabStatus.CHANGED)
        elif kind == ChangeKind.VANISHED:
            tab.error = "File is no longer available"
            self._tabstrip.set_status(key, TabStatus.MISSING)
        else:
            tab.view.on_file_grown()
            if tab.filtering:
                self._scan_filter(tab)
            # Mark a background tab so the user can see which file moved
            # without clicking through them all.
            if key != self._tabstrip.active:
                self._tabstrip.set_status(key, TabStatus.CHANGED)

        if tab.indexer is not None:
            tab.indexer.notify()
        if key == self._tabstrip.active:
            self._update_status()

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _update_status(self) -> None:
        tab = self.active_tab
        if tab is None:
            for label in (self._status_pos, self._status_lines, self._status_size,
                          self._status_encoding):
                label.configure(text="")
            self._set_status("Ready")
            return

        linefile = tab.linefile
        self._status_encoding.configure(text=linefile.codec.label)
        self._status_size.configure(text=_human(linefile.size))

        counted = linefile.line_count
        suffix = "" if linefile.index_complete else "+"
        self._status_lines.configure(text=f"{counted:,}{suffix} lines")

        if tab.filtering:
            shown = tab.filter_view.count
            dropped = tab.filter_view.dropped
            extra = f", {dropped:,} dropped" if dropped else ""
            self._status_pos.configure(text=f"{shown:,} shown{extra}")
        else:
            self._status_pos.configure(text=f"Line {tab.view.top_line_number + 1:,}")

        if tab.error:
            self._set_status(tab.error)
        elif tab.filtering:
            mode = "Including" if tab.filter_mode == FilterMode.INCLUDE else "Excluding"
            self._set_status(f"{mode} lines matching “{tab.filter_matcher.pattern}”"
                             if tab.filter_matcher else "Filtering")
        else:
            state = "Following" if tab.view.following else "Paused"
            self._set_status(f"{tab.path}    [{state}]")

    def _set_status(self, text: str) -> None:
        self._status_text.configure(text=text)

    # ------------------------------------------------------------------
    # View commands
    # ------------------------------------------------------------------

    def _with_view(self, action) -> None:
        tab = self.active_tab
        if tab is not None:
            action(tab.view)
            self._update_status()

    def _toggle_follow(self, toggle: bool = False) -> None:
        if toggle:
            self._follow_var.set(not self._follow_var.get())
        follow = self._follow_var.get()
        tab = self.active_tab
        if tab is not None:
            if follow:
                tab.view.goto_end()
            else:
                tab.view.set_follow(False)
            if tab.filter_view is not None:
                tab.filter_view.set_follow(follow)
        self._update_status()

    def _toggle_wrap(self) -> None:
        wrap = self._wrap_var.get()
        for tab in self._tabs.values():
            tab.view.set_wrap(wrap)
            if tab.filter_view is not None:
                tab.filter_view.set_wrap(wrap)
        self.config_data.set("view.wrap", wrap)

    def _toggle_highlighting(self, toggle: bool = False) -> None:
        if toggle:
            self._highlight_var.set(not self._highlight_var.get())
        self._rules.enabled = self._highlight_var.get()
        self._refresh_views()

    def _refresh_views(self) -> None:
        for tab in self._tabs.values():
            tab.view.set_ruleset(self._rules)
            if tab.filter_view is not None:
                tab.filter_view.set_ruleset(self._rules)

    def goto_line(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        total = tab.linefile.line_count
        number = simpledialog.askinteger(
            "Go to Line", f"Line number (1 – {total:,}):",
            parent=self, minvalue=1, initialvalue=tab.view.top_line_number + 1)
        if number is not None:
            tab.view.goto_line(number - 1)
            self._follow_var.set(False)
            self._update_status()

    def _copy(self) -> None:
        tab = self.active_tab
        if tab is not None and not tab.filtering:
            tab.view.copy_selection()

    def _select_all(self) -> None:
        tab = self.active_tab
        if tab is not None and not tab.filtering:
            tab.view._on_select_all()

    def _copy_all(self) -> None:
        tab = self.active_tab
        if tab is None:
            return
        ok, message = tab.view.copy_all()
        if not ok:
            messagebox.showinfo(APP_NAME, message, parent=self)
        else:
            self._set_status(message)

    # ------------------------------------------------------------------
    # Tab placement
    # ------------------------------------------------------------------

    def _place_tabstrip(self) -> None:
        self._tabstrip.pack_forget()
        self._content.pack_forget()
        side = _SIDE_TO_PACK.get(self._side_var.get(), "top")
        fill = "x" if side in ("top", "bottom") else "y"
        if self._tabs or self.config_data.get("tabs.visible", True):
            self._tabstrip.pack(side=side, fill=fill)
        self._content.pack(side="top", fill="both", expand=True)

    def _apply_tab_placement(self) -> None:
        self._tabstrip.set_side(self._side_var.get())
        self._tabstrip.set_orientation(self._orientation_var.get())
        self._place_tabstrip()

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def edit_highlights(self) -> None:
        edited = HighlightDialog(self, self._rules).show()
        if edited is not None:
            self._rules = edited
            self._highlight_var.set(edited.enabled)
            self._refresh_views()
            self.config_data.store_ruleset(self._rules)

    def choose_font(self) -> None:
        family, size, spacing, offset = self._font_settings()
        chosen = FontDialog(self, family, size, spacing, offset).show()
        if not chosen:
            return
        for key, value in chosen.items():
            self.config_data.set(f"font.{key}", value)
        for tab in self._tabs.values():
            tab.view.set_font(chosen["family"], chosen["size"],
                              chosen["spacing"], chosen["offset"])
            if tab.filter_view is not None:
                tab.filter_view.set_font(chosen["family"], chosen["size"],
                                         chosen["spacing"], chosen["offset"])

    def edit_preferences(self) -> None:
        if PreferencesDialog(self, self.config_data).show() is None:
            return
        get = self.config_data.get
        self._wrap_var.set(bool(get("view.wrap", False)))
        self._side_var.set(get("tabs.side", Side.TOP))
        self._orientation_var.set(get("tabs.orientation", Orientation.HORIZONTAL))
        self._storage_var.set(self.config_data.storage)
        self._apply_tab_placement()

        tab_width = int(get("view.tab_width", 8))
        fg, bg = get("colours.fg", "#000000"), get("colours.bg", "#FFFFFF")
        interval = float(get("poll_interval", 0.25))
        for tab in self._tabs.values():
            tab.view.set_wrap(self._wrap_var.get())
            tab.view.set_tab_width(tab_width)
            tab.view.set_colours(fg, bg)
            if tab.tailer is not None:
                tab.tailer.set_interval(interval)
            if tab.filter_view is not None:
                tab.filter_view.set_wrap(self._wrap_var.get())
                tab.filter_view.set_tab_width(tab_width)
                tab.filter_view.set_colours(fg, bg)

    def show_about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} — a real-time log file viewer.\n\n"
            "A Python reimplementation of BareTail, including the search and\n"
            "filter features of BareTailPro.\n\n"
            "Files of any size are supported: only the lines on screen are\n"
            "ever held in memory.",
            parent=self)

    # ------------------------------------------------------------------
    # Preferences storage
    # ------------------------------------------------------------------

    def _set_storage(self) -> None:
        mode = self._storage_var.get()
        self.config_data.storage = mode
        if mode == cfg.StorageMode.NONE:
            # Choosing not to store should also remove what was stored before,
            # or the old settings would quietly come back next launch.
            cfg.clear_registry()
            self._set_status("Preferences will not be saved")
        else:
            self._set_status(f"Preferences will be stored in the {mode}")

    def load_prefs_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Load preferences",
            filetypes=[("Settings", "*.json"), ("All files", "*.*")])
        if not path:
            return
        if self.config_data.load_from_file(path):
            self._rules = self.config_data.ruleset()
            self._highlight_var.set(self._rules.enabled)
            self._apply_config()
            self._refresh_views()
            self._refresh_recent_menu()
            self._set_status(f"Preferences loaded from {path}")
        else:
            messagebox.showerror(APP_NAME,
                                 f"Could not load preferences.\n\n"
                                 f"{self.config_data.load_error}", parent=self)

    def save_prefs_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Save preferences", defaultextension=".json",
            initialfile=cfg.CONFIG_NAME,
            filetypes=[("Settings", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self._collect_config()
        if self.config_data.save_to_file(path):
            self._set_status(f"Preferences saved to {path}")
        else:
            messagebox.showerror(APP_NAME, f"Could not write {path}", parent=self)

    def _refresh_recent_menu(self) -> None:
        self._recent_menu.delete(0, "end")
        recent = self.config_data.get("recent_files", [])
        if not recent:
            self._recent_menu.add_command(label="(none)", state="disabled")
            return
        for path in recent:
            self._recent_menu.add_command(
                label=path, command=lambda p=path: self.open_file(p))

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def save_session(self) -> None:
        if not self._tabs:
            messagebox.showinfo(APP_NAME, "There are no open files to save.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Save session", defaultextension=".btsession",
            filetypes=[("Session", "*.btsession"), ("All files", "*.*")])
        if not path:
            return
        keys = self._tabstrip.keys
        files = [self._tabs[k].path for k in keys if k in self._tabs]
        active = keys.index(self._tabstrip.active) if self._tabstrip.active in keys else 0
        if cfg.save_session(path, files, active):
            self._set_status(f"Session saved to {path}")
        else:
            messagebox.showerror(APP_NAME, f"Could not write {path}", parent=self)

    def load_session(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Load session",
            filetypes=[("Session", "*.btsession"), ("All files", "*.*")])
        if not path:
            return
        files, active = cfg.load_session(path)
        if not files:
            messagebox.showerror(APP_NAME, f"No files listed in {path}", parent=self)
            return
        for key in list(self._tabs):
            self.close_tab(key)
        missing = []
        for name in files:
            if os.path.exists(name):
                self.open_file(name, activate=False)
            else:
                missing.append(name)
        keys = self._tabstrip.keys
        if keys:
            self._tabstrip.set_active(keys[min(active, len(keys) - 1)])
            self._show_tab(self._tabstrip.active)
        if missing:
            messagebox.showwarning(
                APP_NAME, "These files from the session no longer exist:\n\n" +
                "\n".join(missing), parent=self)

    # ------------------------------------------------------------------
    # Search and filter
    # ------------------------------------------------------------------

    def _ensure_search(self) -> SearchPanel:
        if self._search is None:
            self._search = SearchPanel(
                self._search_holder,
                get_file=lambda: self.active_tab.linefile if self.active_tab else None,
                on_goto=self._goto_offset,
                pump=self._pump,
                on_filter=self._set_filter,
                on_close=self.hide_search,
                saved_patterns=self.config_data.get("saved_searches", []),
                on_save_patterns=lambda p: self.config_data.set("saved_searches", p))
            self._search.pack(fill="both", expand=True)
        return self._search

    def show_search(self) -> None:
        panel = self._ensure_search()
        if not self._search_visible:
            # Packed before the status bar so the results table grows upward
            # into the window rather than pushing the status bar off-screen.
            self._search_holder.pack(side="bottom", fill="both", expand=False,
                                     before=self._status_bar)
            self._search_visible = True
        panel.focus_search()

    def hide_search(self) -> None:
        if self._search_visible:
            self._search_holder.pack_forget()
            self._search_visible = False
            tab = self.active_tab
            if tab is not None:
                (tab.filter_view or tab.view).focus_set()

    def _find(self, backwards: bool) -> None:
        panel = self._ensure_search()
        if not self._search_visible:
            self.show_search()
        panel.find_next(backwards=backwards)

    def _goto_offset(self, offset: int) -> None:
        tab = self.active_tab
        if tab is None:
            return
        tab.view.goto_offset(offset)
        self._follow_var.set(False)
        if tab.filtering:
            # Jumping to a specific line means looking at the file, not the
            # filtered subset.
            self.clear_filter()
        self._update_status()

    def _set_filter(self, matcher: Matcher | None, mode: str) -> None:
        tab = self.active_tab
        if tab is None:
            return
        if matcher is None or mode == "off":
            self.clear_filter()
            return

        tab.filter_matcher = matcher
        tab.filter_mode = mode
        if tab.filter_view is None:
            family, size, spacing, offset = self._font_settings()
            tab.filter_view = FilteredView(
                self._content, self._rules, font_family=family, font_size=size,
                wrap=self._wrap_var.get(),
                tab_width=int(self.config_data.get("view.tab_width", 8)),
                on_activate=self._goto_offset)
            tab.filter_view.set_colours(self.config_data.get("colours.fg", "#000000"),
                                        self.config_data.get("colours.bg", "#FFFFFF"))
        tab.filter_view.reset()
        tab.filter_scanned_to = tab.linefile.content_start
        self._show_tab(tab.key)
        self._scan_filter(tab)

    def _scan_filter(self, tab: FileTab) -> None:
        """Scan the not-yet-filtered part of the file and append what matches.

        Called both when a filter is first applied and each time the file
        grows, which is what makes the filtered view a live tail rather than a
        one-off search.
        """
        if tab.filter_matcher is None or tab.filter_view is None:
            return
        start = tab.filter_scanned_to
        if start >= tab.linefile.size:
            return
        tab.filter_scanned_to = tab.linefile.size

        if tab.filter_engine is None:
            tab.filter_engine = SearchEngine(
                tab.linefile,
                on_hits=lambda batch, key=tab.key:
                    self._pump.post(self._on_filter_hits, key, batch))
        tab.filter_engine.start(tab.filter_matcher, start_offset=start,
                                with_line_numbers=False,
                                invert=tab.filter_mode == FilterMode.EXCLUDE)

    def _on_filter_hits(self, key: str, batch) -> None:
        tab = self._tabs.get(key)
        if tab is None or tab.filter_view is None:
            return
        from ..core.linefile import Line
        tab.filter_view.extend([Line(hit.offset, hit.text) for hit in batch])
        if key == self._tabstrip.active:
            self._update_status()

    def clear_filter(self) -> None:
        tab = self.active_tab
        if tab is None or tab.filter_view is None:
            return
        if tab.filter_engine is not None:
            tab.filter_engine.cancel()
            tab.filter_engine = None
        tab.filter_view.destroy()
        tab.filter_view = None
        tab.filter_matcher = None
        self._show_tab(tab.key)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        self._collect_config()
        if self.config_data.storage != cfg.StorageMode.NONE:
            self.config_data.save()
        for key in list(self._tabs):
            tab = self._tabs.pop(key)
            tab.shutdown()
        self._pump.stop()
        self.destroy()


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


class _Tooltip:
    """A minimal hover tooltip; ttk has none of its own."""

    def __init__(self, widget: tk.Misc, text: str, delay_ms: int = 600) -> None:
        self._widget = widget
        self._text = text
        self._delay = delay_ms
        self._after: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None) -> None:
        self._cancel()
        self._after = self._widget.after(self._delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self._widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        if self._window is not None:
            return
        x = self._widget.winfo_rootx() + 8
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._window = tk.Toplevel(self._widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        tk.Label(self._window, text=self._text, background="#FFFFE1",
                 relief="solid", borderwidth=1, padx=5, pady=2).pack()

    def _hide(self, event=None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None
