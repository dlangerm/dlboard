"""
The basic experiment page: hparam/run table + chart accordion, plus the experiment header.

`serve/_pages/experiment.py`'s `layout()` puts the page together from the parts below; this module's own job is just
`register()`, which registers every part's callbacks.

Layers, each importing only from the ones above it:

- `_page_model.py` -- the persisted page and every pure edit to its panels (no Dash, no store).
- `_page_repository.py` -- loading and saving a page, branching into a view of your own on edit.
- `_panel_data.py` -- fetching a panel's data, and the fingerprint the live poll compares.
- `_ids.py` -- the page tag and the few ids more than one feature reads or writes.
- `_chart_view.py`, `_panel_view.py` -- the panel area's render tree, handed its data (never a store).
- `_page_edits.py` -- "edit, save, re-render", the step every feature's edit callback ends with.

Features, each owning its ids, its modal or drawer, and its callbacks:

- `_panel_controls.py` -- create/rename/delete/move a panel; delete/move a chart.
- `_tab_controls.py` -- create/rename a tab, drop a panel or chart onto one.
- `_chart_editor.py` -- the chart editor docked beside the panel area: a chart is added or edited with the page in view.
- `_chart_settings_form.py` -- the form of a chart type's settings, and reading its values back.
- `_chart_view_state.py` -- "Save as default" for what a viewer changed in a chart without editing it.
- `_chart_suggestions.py` -- auto-populate charts for an empty view; suggest un-charted keys.
- `_runs_table.py` -- the navbar's hparam/metric/run comparison table + run deletion.
- `_panel_area_controls.py` -- paging and filtering the panel area, opening panels, switching tabs.
- `_live_updates.py` -- the poll that redraws only charts whose data changed, and its status.
- `_views.py`, `_notes.py`, `_run_compare.py` -- named views, the notes drawer, the A/B diff.

The page's parts, each putting features together:

- `_panel_area.py` -- the panel area and everything in it, which a comparison's page shows too.
- `_header.py` -- the experiment's header: its name, description and delete, and the page's controls beside them.

Anything outside this package imports straight from the submodule that owns what it needs (the
underscore is a "this is an implementation detail you're reaching into on purpose" signal, not an
access restriction) rather than through re-exports here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.serve._pages._experiment._header import register_header_callbacks
from dlboard.serve._pages._experiment._live_updates import register_live_update_callbacks
from dlboard.serve._pages._experiment._notes import register_notes_callbacks
from dlboard.serve._pages._experiment._panel_area import register_panel_area
from dlboard.serve._pages._experiment._run_compare import register_run_compare_callbacks
from dlboard.serve._pages._experiment._runs_table import register_runs_table_callbacks
from dlboard.serve._pages._experiment._views import register_view_callbacks

if TYPE_CHECKING:
    from dash import Dash


def register(app: Dash) -> None:
    """Register the experiment page: the panel area (a comparison's too), the header, and the experiment's own features."""
    register_panel_area(app)
    register_header_callbacks(app)
    register_live_update_callbacks(app)
    register_runs_table_callbacks(app)
    register_run_compare_callbacks(app)
    register_view_callbacks(app)
    register_notes_callbacks(app)
