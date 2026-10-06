"""
Component-id types, scoped by both Dash-prop role and owning page.

A bare `str` id doesn't stop a callback from reading a `dcc.Store`'s `"data"` off an id that's
actually an `html.Div`, or from reaching across pages to reference another plugin's internal id.
`StoreId[P]`/`DivId[P]`/etc. are `str` subclasses generic over a page-tag type `P` -- each page
plugin declares its own never-instantiated tag class and types its ids against it, so pyright
rejects both a role mismatch (a `DivId` where a `State`/`Input`/`Output` call expects a `StoreId`)
and a page mismatch (`StoreId[_AdminPage]` where `StoreId[_ExperimentPage]` is expected).

These are pure `str` at runtime -- zero behavior change, still valid Dash component ids -- so
using `Input`/`Output`/`State` directly still accepts one, typed against whichever role/page it's
declared for.
"""

from __future__ import annotations


class AppShell:
    """Tag for ids owned by `serve/app.py` itself, not any single page."""


class StoreId[P](str):
    """A `dcc.Store` id -- only `"data"` is a meaningful prop."""


class DivId[P](str):
    """An `html.Div`/`dmc` container id -- only `"children"` is a meaningful prop."""


class ModalId[P](str):
    """A `dmc.Modal`/`dmc.Drawer` id -- only `"opened"` is a meaningful prop."""


class ButtonId[P](str):
    """A `dmc.Button`/`dmc.ActionIcon` id -- only `"n_clicks"` is a meaningful prop."""


class IntervalId[P](str):
    """A `dcc.Interval` id -- only `"n_intervals"` is a meaningful prop."""


class ValueId[P](str):
    """A `TextInput`/`Select`/`NumberInput`/`Switch`-style id -- `"value"` or `"checked"`."""
