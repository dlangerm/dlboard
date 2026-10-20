"""
Component-id types, scoped by both Dash-prop role and owning page.

A bare `str` id doesn't stop a callback from reading a `dcc.Store`'s `"data"` off an id that's
actually an `html.Div`, or from reaching across pages to reference another plugin's internal id.
`StoreId[P]`/`DivId[P]`/etc. are `str` subclasses generic over a page-tag type `P` -- each page
plugin declares its own never-instantiated tag class and types its ids against it, so pyrefly
rejects both a role mismatch (a `DivId` where a `State`/`Input`/`Output` call expects a `StoreId`)
and a page mismatch (`StoreId[_AdminPage]` where `StoreId[_ExperimentPage]` is expected).

These are pure `str` at runtime -- zero behavior change, still valid Dash component ids -- so
using `Input`/`Output`/`State` directly still accepts one, typed against whichever role/page it's
declared for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from dash import ALL, MATCH


class AppShell:
    """Tag for ids owned by `serve/app.py` itself, not any single page."""


class StoreId[P](str):
    """A `dcc.Store` id -- only `"data"` is a meaningful prop."""


class DivId[P](str):
    """An `html.Div`/`dmc` container id -- only `"children"` is a meaningful prop."""


class PaneId[P](str):
    """An `html.Aside`/`html.Div` id for a pane that is shown or hidden -- only `"hidden"` is a meaningful prop."""


class ModalId[P](str):
    """A `dmc.Modal`/`dmc.Drawer` id -- only `"opened"` is a meaningful prop."""


class ButtonId[P](str):
    """A `dmc.Button`/`dmc.ActionIcon` id -- only `"n_clicks"` is a meaningful prop."""


class IntervalId[P](str):
    """A `dcc.Interval` id -- only `"n_intervals"` is a meaningful prop."""


class ValueId[P](str):
    """A `TextInput`/`Select`/`NumberInput`/`Switch`-style id -- `"value"` or `"checked"`."""


@dataclass(frozen=True)
class PatternId[P]:
    """
    A family of pattern-matching ids: every component of one `type`, told apart by the subclass's `KEYS`.

    A subclass names its `KEYS` and gives `of` a typed signature filling them in, to render one component;
    `all()`/`match()` are the same id with every key wildcarded, for a callback over the family. Both come
    from one place, so a callback can't listen for an id the page never renders -- which a hand-written
    `{"type": "...", "panel": ALL}` beside the factory that renders it could, silently, after a typo.
    """

    type: str
    KEYS: ClassVar[tuple[str, ...]]

    def all(self) -> dict[str, Any]:
        """Every component of this family, for an `ALL` callback."""
        return {"type": self.type, **dict.fromkeys(self.KEYS, ALL)}

    def match(self) -> dict[str, Any]:
        """Whichever component of this family triggered the callback, for a `MATCH` callback."""
        return {"type": self.type, **dict.fromkeys(self.KEYS, MATCH)}
