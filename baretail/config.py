"""Preferences and sessions.

BareTail can keep its settings in a file, in the registry, or nowhere at all,
and can load and save them on demand so a set of highlight rules is shareable
between machines and colleagues.  All three modes are here, behind one
interface, so nothing else in the application knows where settings live.

The format is JSON.  Settings are merged over :data:`DEFAULTS` on load, which
means a file written by an older version stays readable and a hand-edited file
missing a key still works.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .core.highlight import DEFAULT_RULES, RuleSet

__all__ = ["Config", "StorageMode", "DEFAULTS", "config_dir"]


class StorageMode:
    FILE = "file"
    REGISTRY = "registry"
    NONE = "none"


#: Registry location used by :data:`StorageMode.REGISTRY`.
REGISTRY_KEY = r"Software\BareTailClone"
REGISTRY_VALUE = "Settings"

#: Default settings file name, kept beside the application so a copy on a USB
#: stick or network share carries its configuration with it.
CONFIG_NAME = "baretail.json"

DEFAULTS: dict[str, Any] = {
    "storage": StorageMode.FILE,
    "window": {"x": None, "y": None, "width": 1000, "height": 700, "state": 0},
    "font": {"family": "Consolas", "size": 10, "spacing": 0, "offset": 0},
    "view": {
        "wrap": False,
        "tab_width": 8,
        "follow": True,
        "highlights_enabled": True,
        "show_line_numbers": False,
    },
    "colours": {"fg": "#000000", "bg": "#FFFFFF"},
    "tabs": {"side": "top", "orientation": "horizontal", "visible": True},
    "encoding": {"default": "ANSI"},
    "poll_interval": 0.25,
    "highlights": None,          # None means "use DEFAULT_RULES"
    "saved_searches": [],
    "recent_files": [],
    "search": {"regex": False, "case_sensitive": False, "whole_word": False},
    "filter": {"mode": "include"},
}

#: Cap on the recent-files list.
MAX_RECENT = 12


def config_dir() -> str:
    """Directory the application lives in.

    BareTail is a single executable that runs from anywhere, including a
    network share, and looks for its settings alongside itself.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path() -> str:
    return os.path.join(config_dir(), CONFIG_NAME)


def _deep_merge(base: dict, incoming: dict) -> dict:
    """Merge ``incoming`` over ``base``, recursing into nested dicts.

    Keeps unknown keys from ``incoming`` -- a settings file written by a newer
    version is not silently stripped when an older one saves it back.
    """
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Application settings, with pluggable storage.

    Values are reached through :meth:`get` and :meth:`set` using dotted paths
    (``"font.family"``), so callers never have to guard against a missing
    intermediate dict.
    """

    def __init__(self, data: dict | None = None, path: str | None = None) -> None:
        self.data = _deep_merge(DEFAULTS, data or {})
        self.path = path or default_config_path()
        #: Set when a load failed, so the UI can say so rather than silently
        #: reverting to defaults.
        self.load_error: str | None = None

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def get(self, dotted: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    @property
    def storage(self) -> str:
        return self.get("storage", StorageMode.FILE)

    @storage.setter
    def storage(self, mode: str) -> None:
        self.set("storage", mode)

    # ------------------------------------------------------------------
    # Highlight rules
    # ------------------------------------------------------------------

    def ruleset(self) -> RuleSet:
        """The saved rules, or the shipped defaults if none were ever saved.

        An explicitly empty list is honoured -- a user who deleted every rule
        gets no rules, not the defaults back.
        """
        stored = self.get("highlights")
        enabled = bool(self.get("view.highlights_enabled", True))
        if stored is None:
            return RuleSet([rule.copy() for rule in DEFAULT_RULES], enabled=enabled)
        return RuleSet.from_list(stored, enabled=enabled)

    def store_ruleset(self, rules: RuleSet) -> None:
        self.set("highlights", rules.to_list())
        self.set("view.highlights_enabled", rules.enabled)

    # ------------------------------------------------------------------
    # Recent files
    # ------------------------------------------------------------------

    def add_recent(self, path: str) -> None:
        """Move ``path`` to the front of the recent list."""
        recent = [p for p in self.get("recent_files", []) if p and p != path]
        recent.insert(0, path)
        self.set("recent_files", recent[:MAX_RECENT])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        """Load settings, preferring a file and falling back to the registry.

        A file beside the application wins, so a portable copy carries its own
        settings even on a machine whose registry holds different ones.
        """
        target = path or default_config_path()
        if os.path.exists(target):
            config = cls(path=target)
            config._read_file(target)
            return config

        data = _read_registry()
        if data is not None:
            config = cls(data, path=target)
            config.storage = StorageMode.REGISTRY
            return config

        return cls(path=target)

    def _read_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("settings file is not a JSON object")
            self.data = _deep_merge(DEFAULTS, loaded)
            self.load_error = None
        except (OSError, ValueError) as exc:
            # Defaults are already in place; report rather than crash, so a
            # corrupt settings file never stops the viewer from opening.
            self.load_error = f"{path}: {exc}"

    def load_from_file(self, path: str) -> bool:
        """Replace the current settings from ``path``. Returns success."""
        self._read_file(path)
        if self.load_error is None:
            self.path = path
            return True
        return False

    def save(self) -> bool:
        """Persist according to the configured storage mode."""
        mode = self.storage
        if mode == StorageMode.NONE:
            return True
        if mode == StorageMode.REGISTRY:
            return _write_registry(self.data)
        return self.save_to_file(self.path)

    def save_to_file(self, path: str) -> bool:
        """Write settings to ``path``.

        Written to a temporary file and moved into place, so an interrupted
        save cannot leave a half-written settings file that fails to load next
        time.
        """
        temp = path + ".tmp"
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(temp, path)
            return True
        except OSError:
            try:
                os.remove(temp)
            except OSError:
                pass
            return False


# ----------------------------------------------------------------------
# Registry backend
# ----------------------------------------------------------------------

def _read_registry() -> dict | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def _write_registry(data: dict) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ,
                              json.dumps(data, sort_keys=True))
        return True
    except OSError:
        return False


def clear_registry() -> bool:
    """Remove stored settings from the registry.

    Used when switching to "do not store", so choosing not to persist actually
    removes what was persisted before rather than leaving it behind.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

def save_session(path: str, files: list[str], active: int = 0) -> bool:
    """Write the set of open files to a session file."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "files": files, "active": active}, fh, indent=2)
        return True
    except OSError:
        return False


def load_session(path: str) -> tuple[list[str], int]:
    """Read a session file, returning its files and the active index."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        files = [str(p) for p in data.get("files", []) if isinstance(p, str)]
        active = int(data.get("active", 0))
        return files, max(0, min(active, max(0, len(files) - 1)))
    except (OSError, ValueError, TypeError):
        return [], 0
