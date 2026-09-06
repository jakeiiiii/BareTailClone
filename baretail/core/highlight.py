"""Whole-line highlighting rules.

BareTail colours an entire line when it contains a given string.  Rules are
tried in the order they appear in the list and **the first one that matches
wins** -- so a specific rule must be placed above a general one, and moving a
rule up or down is how the user resolves an overlap.

Matching only ever runs against the lines currently on screen, so the cost is
bounded by the size of the viewport rather than the size of the file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable

__all__ = ["HighlightRule", "RuleSet", "DEFAULT_RULES", "DEFAULT_COLOURS"]

#: Colours for text matching no rule at all.
DEFAULT_COLOURS = {"fg": "#000000", "bg": "#FFFFFF"}


@dataclass
class HighlightRule:
    """One colouring rule.

    ``pattern`` is a literal substring unless ``is_regex`` is set.  An invalid
    regex is kept rather than discarded -- the user is probably mid-edit -- but
    it matches nothing and reports its error through :attr:`error`.
    """

    pattern: str = ""
    fg: str = "#000000"
    bg: str = "#FFFF80"
    enabled: bool = True
    is_regex: bool = False
    case_sensitive: bool = False

    _compiled: re.Pattern | None = field(default=None, repr=False, compare=False)
    _compiled_for: tuple | None = field(default=None, repr=False, compare=False)
    error: str | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------

    def _ensure_compiled(self) -> re.Pattern | None:
        """Compile lazily, and only when the rule's inputs have changed.

        The rule is a mutable dataclass edited directly by the dialog, so the
        cache is keyed on the fields that affect matching.
        """
        key = (self.pattern, self.is_regex, self.case_sensitive)
        if self._compiled_for == key:
            return self._compiled

        self._compiled_for = key
        self.error = None
        if not self.pattern:
            self._compiled = None
            return None

        flags = 0 if self.case_sensitive else re.IGNORECASE
        source = self.pattern if self.is_regex else re.escape(self.pattern)
        try:
            self._compiled = re.compile(source, flags)
        except re.error as exc:
            self._compiled = None
            self.error = str(exc)
        return self._compiled

    @property
    def valid(self) -> bool:
        """False only for a regex that does not compile."""
        self._ensure_compiled()
        return self.error is None

    def matches(self, text: str) -> bool:
        """True if this rule should colour ``text``.

        A disabled rule, an empty pattern and a broken regex all match
        nothing, so they fall through to whatever rule comes next.
        """
        if not self.enabled:
            return False
        compiled = self._ensure_compiled()
        if compiled is None:
            return False
        return compiled.search(text) is not None

    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "fg": self.fg,
            "bg": self.bg,
            "enabled": self.enabled,
            "regex": self.is_regex,
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HighlightRule":
        return cls(
            pattern=str(data.get("pattern", "")),
            fg=str(data.get("fg", "#000000")),
            bg=str(data.get("bg", "#FFFF80")),
            enabled=bool(data.get("enabled", True)),
            is_regex=bool(data.get("regex", False)),
            case_sensitive=bool(data.get("case_sensitive", False)),
        )

    def copy(self) -> "HighlightRule":
        # Drop the compilation cache so the copy recompiles on first use.
        return replace(self, _compiled=None, _compiled_for=None, error=None)


class RuleSet:
    """An ordered list of rules, resolved first-match-wins."""

    def __init__(self, rules: Iterable[HighlightRule] | None = None,
                 enabled: bool = True) -> None:
        self.rules: list[HighlightRule] = list(rules or [])
        #: Master switch behind the toolbar's highlight toggle.
        self.enabled = enabled

    def __len__(self) -> int:
        return len(self.rules)

    def __iter__(self):
        return iter(self.rules)

    def __getitem__(self, index: int) -> HighlightRule:
        return self.rules[index]

    # ------------------------------------------------------------------

    def match(self, text: str) -> HighlightRule | None:
        """The first rule that matches ``text``, or None."""
        if not self.enabled:
            return None
        for rule in self.rules:
            if rule.matches(text):
                return rule
        return None

    def match_index(self, text: str) -> int:
        """Index of the first matching rule, or -1.

        The viewport uses the index as a stable tag name, which avoids
        reconfiguring a tag per line on every repaint.
        """
        if not self.enabled:
            return -1
        for index, rule in enumerate(self.rules):
            if rule.matches(text):
                return index
        return -1

    # ------------------------------------------------------------------

    def move(self, index: int, delta: int) -> int:
        """Move a rule up or down, returning its new index.

        Order is meaningful -- it decides which of two overlapping rules
        applies -- so this is a first-class operation rather than a
        convenience.
        """
        target = max(0, min(len(self.rules) - 1, index + delta))
        if target != index:
            self.rules.insert(target, self.rules.pop(index))
        return target

    def to_list(self) -> list[dict]:
        return [rule.to_dict() for rule in self.rules]

    @classmethod
    def from_list(cls, data, enabled: bool = True) -> "RuleSet":
        rules = [HighlightRule.from_dict(item) for item in (data or [])
                 if isinstance(item, dict)]
        return cls(rules, enabled=enabled)

    def copy(self) -> "RuleSet":
        return RuleSet([rule.copy() for rule in self.rules], enabled=self.enabled)


#: Shipped defaults, ordered most severe first so that a line reading
#: "ERROR: warning suppressed" is coloured as the error it is.
DEFAULT_RULES = [
    HighlightRule("FATAL", fg="#FFFFFF", bg="#C00000"),
    HighlightRule("ERROR", fg="#FFFFFF", bg="#E04040"),
    HighlightRule("WARN", fg="#000000", bg="#FFD040"),
    HighlightRule("INFO", fg="#000000", bg="#D8F0D8", enabled=False),
    HighlightRule("DEBUG", fg="#606060", bg="#FFFFFF", enabled=False),
]
