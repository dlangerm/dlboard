"""
The comparison page: a dashboard of hand-picked runs from any experiments of one project.

It is the experiment page's panel area (tabs, panels, charts, the chart editor, suggestions) over a different set of runs,
so it reuses `_experiment`'s pieces and adds what is a comparison's own:

- `_header.py` -- its name, description and owner, and sharing, duplicating and deleting it;
- `_members.py` -- its member runs, as the run list in the sidebar: hide one from the charts, take some out;
- `_add_runs.py` -- adding runs to it, from the pinned ones or from an experiment.

Its page is a `Page` scoped to a project (`ComparisonRef`), shown at `/comparison/<id>`. It has no live updates and no
A/B diff yet: those read one experiment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.serve._pages._comparison._add_runs import register_add_runs_callbacks
from dlboard.serve._pages._comparison._header import register_header_callbacks
from dlboard.serve._pages._comparison._members import register_members_callbacks

if TYPE_CHECKING:
    from dash import Dash


def register(app: Dash) -> None:
    """Register the comparison page's callbacks: the shared panel area's are registered with the experiment page's."""
    register_header_callbacks(app)
    register_members_callbacks(app)
    register_add_runs_callbacks(app)
