"""Names and `*` patterns: how a chart's settings ask for every metric, hyperparameter or artifact that matches."""

from __future__ import annotations

import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    from collections.abc import Collection

_WILDCARD: typing.Final = "*"


def is_pattern(text: str) -> bool:
    """Whether `text` is a pattern (it has a `*` in it), rather than a name."""
    return _WILDCARD in text


def matches(pattern: str, key: str) -> bool:
    """
    Whether `key` is one `pattern` names. A `*` stands for any run of characters, `/` included, and none at all.

    A pattern without a `*` names only itself. Matching is case-sensitive, as keys are, and is done here rather than
    by the database, whose `LIKE` folds case on some and whose collations order keys differently on others.
    """
    first, *rest = pattern.split(_WILDCARD)
    if not rest:
        return key == pattern
    *inner, last = rest
    end = len(key) - len(last)
    if end < len(first) or not key.startswith(first) or not key.endswith(last):
        return False
    position = len(first)
    for part in inner:
        found = key.find(part, position, end)
        if found < 0:
            return False
        position = found + len(part)
    return position <= end


@dataclass(frozen=True)
class KeyPatterns:
    """
    The keys a chart asks for: names (`loss`) and patterns, where `*` stands for any run of characters.

    `eval/*` and `layer_*/grad_norm` are patterns, and `*` alone is every key. A pattern matches keys logged
    later too, so a chart set up before training started shows what it logs.
    """

    patterns: tuple[str, ...]

    @property
    def exact(self) -> frozenset[str]:
        """The names asked for outright."""
        return frozenset(p for p in self.patterns if not is_pattern(p))

    @property
    def wildcards(self) -> frozenset[str]:
        """The patterns asked for, each with a `*` in it."""
        return frozenset(p for p in self.patterns if is_pattern(p))

    @property
    def everything(self) -> bool:
        """Whether `*` alone is asked for: every key, however many there are."""
        return _WILDCARD in self.patterns

    def matches(self, key: str) -> bool:
        """Whether `key` is one of the names or patterns asked for."""
        return any(matches(pattern, key) for pattern in self.patterns)

    def select(self, keys: Collection[str], *, reserved: Collection[str] = ()) -> list[str]:
        """
        The `keys` these ask for, in the order they ask for them: a name where it exists, a pattern's keys in `keys` order.

        A key in `reserved` is only ever selected by name, never by a pattern (so `*` is not `step` too).
        """
        selected: dict[str, None] = {}
        for pattern in self.patterns:
            if is_pattern(pattern):
                selected |= dict.fromkeys(k for k in keys if matches(pattern, k) and k not in reserved)
            elif pattern in keys:
                selected[pattern] = None
        return list(selected)
