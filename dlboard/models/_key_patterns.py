"""Names and patterns: how a chart's settings ask for every metric, hyperparameter or artifact that matches."""

from __future__ import annotations

import functools
import re
import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    from collections.abc import Collection

_ANY: typing.Final = "*"
_DIGITS: typing.Final = "#"
_TOKENS: typing.Final = re.compile(r"([*#])")
_DIGIT_RUN: typing.Final = re.compile(r"\d+")


def is_pattern(text: str) -> bool:
    """Whether `text` is a pattern (it has a `*` or a `#` in it), rather than a name."""
    return _TOKENS.search(text) is not None


def literal_prefix(pattern: str) -> str:
    """The text a pattern starts with before its first `*` or `#`: what every key it matches starts with."""
    return _TOKENS.split(pattern, maxsplit=1)[0]


def family_template(key: str) -> str:
    """
    The pattern that names `key` and every key like it: each run of digits replaced by `#`.

    `layer_3/grad_norm` and `layer_17/grad_norm` share `layer_#/grad_norm`, which names those and no other key (a
    template holds no digit, so a key matches it only if its own template is the same). A key with a wildcard
    character in it is its own family, since it could not be told from the pattern.
    """
    return key if is_pattern(key) else _DIGIT_RUN.sub(_DIGITS, key)


@functools.cache
def _compiled(pattern: str) -> re.Pattern[str]:
    """The pattern as a regular expression with a group for each `*` (lazily, as little as it can) and `#`."""
    parts = _TOKENS.split(re.sub(r"\*+", _ANY, pattern))
    # `split` keeps the tokens, at the odd positions; the even ones are the text between them.
    regex = "".join(
        re.escape(part) if at % 2 == 0 else r"(.*?)" if part == _ANY else r"(\d+)"
        for at, part in enumerate(parts)
    )
    return re.compile(regex, re.DOTALL)


def captures(pattern: str, key: str) -> tuple[str, ...] | None:
    """
    What each `*` and `#` of `pattern` stood for in `key`, in order, or `None` if `key` is not one it names.

    A key that is the pattern's own text matches it with no captures, so a metric whose name has a `#` in it
    can still be asked for by name. Adjacent `*`s are one.
    """
    if pattern == key:
        return ()
    found = _compiled(pattern).fullmatch(key)
    return None if found is None else found.groups()


def matches(pattern: str, key: str) -> bool:
    """
    Whether `key` is one `pattern` names.

    A `*` stands for any run of characters, `/` included, and none at all. A `#` stands for a run of digits (at
    least one), so `accuracy/#,#/dog` names the cells of a grid and not `accuracy/3,7/aux/dog`. A pattern with
    neither names only itself. Matching is case-sensitive, as keys are, and is done here rather than by the
    database, whose `LIKE` folds case on some and whose collations order keys differently on others.
    """
    return captures(pattern, key) is not None


@dataclass(frozen=True)
class KeyPatterns:
    """
    The keys a chart asks for: names (`loss`) and patterns.

    A `*` stands for any run of characters and a `#` for a run of digits: `eval/*`, `layer_*/grad_norm` and
    `accuracy/#,#/*` are patterns, and `*` alone is every key. A pattern matches keys logged later too, so a chart
    set up before training started shows what it logs.
    """

    patterns: tuple[str, ...]

    @property
    def exact(self) -> frozenset[str]:
        """The names asked for outright."""
        return frozenset(p for p in self.patterns if not is_pattern(p))

    @property
    def wildcards(self) -> frozenset[str]:
        """The patterns asked for, each with a `*` or a `#` in it."""
        return frozenset(p for p in self.patterns if is_pattern(p))

    @property
    def everything(self) -> bool:
        """Whether `*` alone is asked for: every key, however many there are."""
        return _ANY in self.patterns

    def matches(self, key: str) -> bool:
        """Whether `key` is one of the names or patterns asked for."""
        return any(matches(pattern, key) for pattern in self.patterns)

    def capture(self, key: str) -> tuple[str, ...] | None:
        """What the wildcards of the first pattern that names `key` stood for in it (see `captures`), or `None`."""
        return next((got for pattern in self.patterns if (got := captures(pattern, key)) is not None), None)

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
