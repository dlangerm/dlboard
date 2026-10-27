"""
A picker of an experiment's runs: a page of them listed, and the rest searched on the server as someone types (#374).

An experiment's runs are unbounded, so a picker never lists every one: it offers the newest `RUN_PICKER_LIMIT`, or the
ones a search finds by name (or id), and always the runs already picked, so they keep their labels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from dash import Input, Output, State

from dlboard.models import Run
from dlboard.serve import _constants as constants
from dlboard.serve import get_data_store
from dlboard.serve._pages._dataframe_helpers import run_display_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash

    from dlboard.models import DataStore

RUN_PICKER_LIMIT: Final = 50
"""The most runs a picker lists at once: the newest, or the ones its search finds. Every run is a search away."""


def run_options(
    store: DataStore[...], experiment_id: int, *, search: str = "", keep: Iterable[str] = ()
) -> list[dict[str, str]]:
    """A picker's options: the runs it has picked (`keep`, as its values) and the newest `RUN_PICKER_LIMIT` `search` finds."""
    found = list(store.get_runs(experiment_id, limit=RUN_PICKER_LIMIT, run_ids=None, name_contains=search))
    kept = frozenset(int(value) for value in keep) - {run.id for run in found}
    picked = list(store.get_runs(experiment_id, limit=None, run_ids=kept)) if kept else list[Run]()
    return [{"value": str(run.id), "label": run_display_name(run)} for run in [*picked, *found]]


def register_run_search(app: Dash, picker: str) -> None:
    """Search the runs `picker` (a searchable `MultiSelect` of run ids) lists on the server, as someone types in it."""

    @app.callback(
        Output(picker, "data", allow_duplicate=True),
        Input(picker, "searchValue"),
        State(picker, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def search_runs(search: str | None, picked: list[str] | None, experiment_id: int) -> list[dict[str, str]]:
        return run_options(get_data_store(), experiment_id, search=search or "", keep=picked or ())
