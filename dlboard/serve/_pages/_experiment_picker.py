"""
A picker of a project's experiments: a page of them listed, and the rest searched on the server as someone types (#590).

A project's experiments are unbounded, so a picker never lists every one: it offers the newest `EXPERIMENT_PICKER_LIMIT`,
or the ones a search finds by name (or id), and always the experiments already picked, so they keep their labels. The
experiment picker of the run picker's (`_experiment/_run_picker.py`) kind.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from dash import Input, Output, State, dcc

from dlboard.serve import ClientsideScript, get_data_store
from dlboard.serve._pages._dataframe_helpers import experiment_display_name

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash

    from dlboard.models import DataStore

EXPERIMENT_PICKER_LIMIT: Final = 50
"""The most experiments a picker lists at once: the newest, or the ones its search finds. Every one is a search away."""


def experiment_options(
    store: DataStore[...], project_id: int, *, search: str = "", keep: Iterable[str] = ()
) -> list[dict[str, str]]:
    """A picker's options: the experiments it has picked (`keep`, as its values) and the newest `search` finds."""
    found = store.search_experiments(
        project_id, name_contains=search, limit=EXPERIMENT_PICKER_LIMIT
    ).experiments
    kept = frozenset(int(value) for value in keep if value.isdigit()) - {e.id for e in found}
    picked = [e for e in store.get_experiments_by_id(kept) if e.project_id == project_id]
    return [{"value": str(e.id), "label": experiment_display_name(e)} for e in [*picked, *found]]


def picked_values(value: str | list[str] | None) -> list[str]:
    """What a `Select` (one value, or none) or a `MultiSelect` (a list) has picked, as a list."""
    match value:
        case None:
            return []
        case str():
            return [value]
        case list():
            return value


def searched(picker: str) -> str:
    """The id of the store `picker`'s typed search is kept in (`experiment_search_store`), for a search callback's `Input`."""
    return f"{picker}-searched"


def experiment_search_store(picker: str) -> dcc.Store:
    """Where `picker`'s typed search is kept for the server to search: put it in the layout beside the picker."""
    return dcc.Store(id=searched(picker))


_SEARCH_JS: Final = ClientsideScript(Path(__file__).with_name("experiment_search.js"))


def register_search_gate(app: Dash, picker: str) -> None:
    """
    Keep only what someone types in `picker` in its store (`searched`), for a server search to listen to.

    A picker drawn by a callback sets its own search as it mounts (`experiment_search.js`), which would otherwise cost
    every load of its page a search nobody made.
    """
    app.clientside_callback(
        _SEARCH_JS.source,
        Output(searched(picker), "data"),
        Input(picker, "searchValue"),
        State(picker, "value"),
        State(picker, "data"),
        State(searched(picker), "data"),
        prevent_initial_call=True,
    )


def register_experiment_search(app: Dash, picker: str, project: str) -> None:
    """Search the experiments `picker` (a searchable `Select` or `MultiSelect` of experiment ids) lists, as someone types."""
    register_search_gate(app, picker)

    @app.callback(
        Output(picker, "data", allow_duplicate=True),
        Input(searched(picker), "data"),
        State(picker, "value"),
        State(project, "data"),
        prevent_initial_call=True,
    )
    def search_experiments(
        search: str | None, picked: str | list[str] | None, project_id: int | None
    ) -> list[dict[str, str]]:
        if project_id is None:
            return []
        return experiment_options(
            get_data_store(), project_id, search=search or "", keep=picked_values(picked)
        )
