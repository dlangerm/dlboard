"""Names and `prefix*` patterns: how a chart's settings ask for every metric, hyperparameter or artifact under a prefix."""

from __future__ import annotations

import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    from collections.abc import Collection

_WILDCARD: typing.Final = "*"


@dataclass(frozen=True)
class KeyPatterns:
    """
    A list of keys to show: each a name (`loss`), or a prefix and a `*` (`eval/*`; `*` alone being every key).

    A prefix matches keys logged later too, so a chart set up before training started shows what it logs.
    """

    patterns: tuple[str, ...]

    @property
    def exact(self) -> frozenset[str]:
        """The names asked for outright."""
        return frozenset(p for p in self.patterns if not p.endswith(_WILDCARD))

    @property
    def prefixes(self) -> frozenset[str]:
        """The prefixes asked for, `""` being every key."""
        return frozenset(p.removesuffix(_WILDCARD) for p in self.patterns if p.endswith(_WILDCARD))

    def select(self, keys: Collection[str], *, reserved: Collection[str] = ()) -> list[str]:
        """
        The `keys` these ask for, in the order they ask for them: a name where it exists, a prefix's keys in `keys` order.

        A key in `reserved` is only ever selected by name, never by a prefix (so `*` is not `step` too).
        """
        selected: dict[str, None] = {}
        for pattern in self.patterns:
            if pattern.endswith(_WILDCARD):
                prefix = pattern.removesuffix(_WILDCARD)
                selected |= dict.fromkeys(k for k in keys if k.startswith(prefix) and k not in reserved)
            elif pattern in keys:
                selected[pattern] = None
        return list(selected)
