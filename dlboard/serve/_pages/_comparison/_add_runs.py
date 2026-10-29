"""
Adding runs to a comparison: every run of the project pinned in some colours, or runs picked from one experiment.

Both end the way a run being taken out does (`_members.membership_changed`): the list of runs and the charts are drawn
again from the stored page, and nothing is saved to it. A comparison that would hold more than its limit refuses the
whole addition, and says so.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from dlboard.models import PinColor
from dlboard.serve import ClientsideScript, get_current_user, get_data_store
from dlboard.serve._backend._comparison_settings import ComparisonSettings
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, ValueId
from dlboard.serve._pages._comparison import _ids as ids
from dlboard.serve._pages._comparison._members import membership_changed, notice
from dlboard.serve._pages._dash_helpers import CallbackStates, section_label
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._experiment import _ids as experiment_ids
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment._run_picker import run_options
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID
from dlboard.serve._pages._pins._pin_view import pin_glyph

if TYPE_CHECKING:
    from collections.abc import Collection

    from dash.development.base_component import Component

    from dlboard.models import DataStore

ADD_RUNS_MODAL_ID: ModalId[ids.ComparisonPage] = ModalId("comparison-add-modal")
PIN_COLORS_ID: ValueId[ids.ComparisonPage] = ValueId("comparison-add-colors")
PINNED_COUNT_ID: DivId[ids.ComparisonPage] = DivId("comparison-add-count")
ADD_PINNED_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-add-pinned")
EXPERIMENT_ID: ValueId[ids.ComparisonPage] = ValueId("comparison-add-experiment")
RUNS_ID: ValueId[ids.ComparisonPage] = ValueId("comparison-add-runs")
ADD_PICKED_ID: ButtonId[ids.ComparisonPage] = ButtonId("comparison-add-picked")


def _count_text(found: int, colors: Collection[PinColor]) -> str:
    """How many runs the pinned colours would add: said before it is asked, because a comparison holds only so many."""
    cap = ComparisonSettings().max_comparison_runs
    if found > cap:
        return f"More than {cap} runs: a comparison holds at most {cap}."
    return f"{found} run{'s' if found != 1 else ''} pinned" + (" in these colours" if colors else "")


def add_runs_modal(store: DataStore[...], project_id: int) -> Component:
    """The modal to add runs in, in the page itself: what it adds them to is drawn again around it."""
    counts = store.pin_counts(project_id)
    found = len(store.pinned_run_ids(project_id, limit=ComparisonSettings().max_comparison_runs + 1))
    return dmc.Modal(
        id=ADD_RUNS_MODAL_ID,
        title="Add runs",
        opened=False,
        size="lg",
        children=[
            section_label("Pinned runs"),
            dmc.Text(
                "Every run of this project pinned in the colours you pick, whoever pinned it: none picked means any pinned run.",
                size="sm",
                c="dimmed",
                mb="xs",
            ),
            dmc.ChipGroup(
                dmc.Group(
                    [
                        dmc.Chip(
                            pin_glyph(color),
                            value=color.value,
                            variant="outline",
                            **typing.cast("dict[str, Any]", {"aria-label": f"Pinned {color.value}"}),
                        )
                        for color in PinColor
                        if color in counts
                    ],
                    gap="xs",
                    mb="xs",
                ),
                id=PIN_COLORS_ID,
                multiple=True,
                value=[],
            ),
            dmc.Group(
                [
                    dmc.Text(
                        _count_text(found, []), id=PINNED_COUNT_ID, size="sm", c="dimmed", style={"flex": 1}
                    ),
                    dmc.Button("Add pinned runs", id=ADD_PINNED_ID, n_clicks=0),
                ],
                wrap="nowrap",
            ),
            dmc.Divider(my="md"),
            section_label("From an experiment"),
            dmc.Select(
                id=EXPERIMENT_ID,
                data=[
                    {"value": str(e.id), "label": experiment_display_name(e)}
                    for e in store.get_experiments(project_id)
                ],
                placeholder="Pick an experiment",
                searchable=True,
                mb="xs",
                **typing.cast("dict[str, Any]", {"aria-label": "Experiment"}),
            ),
            dmc.Group(
                [
                    dmc.MultiSelect(
                        id=RUNS_ID,
                        data=[],
                        placeholder="Search its runs",
                        searchable=True,
                        clearable=True,
                        size="sm",
                        style={"flex": 1},
                        **typing.cast("dict[str, Any]", {"aria-label": "Runs"}),
                    ),
                    dmc.Button("Add selected runs", id=ADD_PICKED_ID, n_clicks=0),
                ],
                align="flex-end",
                wrap="nowrap",
            ),
        ],
    )


class _Add(CallbackStates):
    clicks: Annotated[int | None, Input(ADD_PINNED_ID, "n_clicks")]
    colors: Annotated[list[PinColor], State(PIN_COLORS_ID, "value")]
    page_json: Annotated[str, State(experiment_ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(experiment_ids.PANEL_PAGING_ID, "data")]


class _AddPicked(CallbackStates):
    clicks: Annotated[int | None, Input(ADD_PICKED_ID, "n_clicks")]
    runs: Annotated[list[int], State(RUNS_ID, "value")]
    page_json: Annotated[str, State(experiment_ids.STATE_PAGE_STORAGE, "data")]
    paging_json: Annotated[str, State(experiment_ids.PANEL_PAGING_ID, "data")]


class _Count(CallbackStates):
    colors: Annotated[list[PinColor], Input(PIN_COLORS_ID, "value")]
    page_json: Annotated[str, State(experiment_ids.STATE_PAGE_STORAGE, "data")]


def _pinned_runs(colors: Collection[PinColor], page_json: str) -> set[int]:
    """
    The runs pinned in the colours asked for (any, if none): one more than a comparison can hold at most.

    That many is enough to refuse the addition, which says how many fit; no use reading further to say so.
    """
    project_id = typing.cast("int", page_model.BasicExperimentPage.model_validate_json(page_json).project_id)
    return set(
        get_data_store().pinned_run_ids(
            project_id,
            colors=frozenset(colors) or None,
            limit=ComparisonSettings().max_comparison_runs + 1,
        )
    )


def _add(run_ids: Collection[int], page_json: str, paging_json: str) -> tuple[Any, ...]:
    """Add `run_ids` to the comparison on screen: its panel area, page, revision and a toast, and whether the modal closes."""
    store = get_data_store()
    held = page_model.BasicExperimentPage.model_validate_json(page_json)
    try:
        added = store.add_comparison_runs(held.id, frozenset(run_ids), added_by=get_current_user().id)
    except PermissionError:
        return (
            no_update,
            no_update,
            no_update,
            notice(
                "Not yours to change",
                "Only its owner, or an editor of a shared comparison, can add runs to it.",
                color="red",
            ),
            False,
        )
    except ValueError as refused:
        return no_update, no_update, no_update, notice("Not added", str(refused), color="red"), False
    if not added:
        return (
            no_update,
            no_update,
            no_update,
            notice("Nothing new", "Those runs are already in it.", color="yellow"),
            False,
        )
    message = f"Added {added} run{'s' if added != 1 else ''}."
    return (*membership_changed(page_json, paging_json), notice("Runs added", message), True)


_OPEN_ON_CLICK_JS = ClientsideScript(Path(__file__).with_name("open_on_click.js"))


def register_add_runs_callbacks(app: Dash) -> None:
    """Open the modal, offer an experiment's runs, say how many a colour would add, and add pinned or picked runs."""
    app.clientside_callback(
        _OPEN_ON_CLICK_JS.source,
        Output(ADD_RUNS_MODAL_ID, "opened"),
        Input(ids.ADD_RUNS_OPEN_ID, "n_clicks", allow_optional=True),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(PINNED_COUNT_ID, "children"),
        _Count.states(),
        prevent_initial_call=True,
    )
    def count_pinned(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> str:
        counted = _Count.model_validate(states)
        return _count_text(len(_pinned_runs(counted.colors, counted.page_json)), counted.colors)

    @app.callback(
        Output(RUNS_ID, "data"),
        Output(RUNS_ID, "value"),
        Input(EXPERIMENT_ID, "value"),
        prevent_initial_call=True,
    )
    def offer_runs(experiment: str | None) -> tuple[list[dict[str, str]], list[str]]:
        options: list[dict[str, str]] = run_options(get_data_store(), int(experiment)) if experiment else []
        return options, list[str]()

    @app.callback(
        Output(RUNS_ID, "data", allow_duplicate=True),
        Input(RUNS_ID, "searchValue"),
        State(EXPERIMENT_ID, "value"),
        State(RUNS_ID, "value"),
        prevent_initial_call=True,
    )
    def search_runs(
        search: str | None, experiment: str | None, picked: list[str] | None
    ) -> list[dict[str, str]]:
        if not experiment:
            raise PreventUpdate
        return run_options(get_data_store(), int(experiment), search=search or "", keep=picked or ())

    outputs = (
        Output(experiment_ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(experiment_ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(ids.MEMBERS_REVISION_ID, "data", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
        Output(ADD_RUNS_MODAL_ID, "opened", allow_duplicate=True),
    )

    @app.callback(*outputs, _Add.states(), prevent_initial_call=True)
    def add_pinned(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Any, ...]:
        add = _Add.model_validate(states)
        if not add.clicks:
            raise PreventUpdate
        return _add(_pinned_runs(add.colors, add.page_json), add.page_json, add.paging_json)

    @app.callback(*outputs, _AddPicked.states(), prevent_initial_call=True)
    def add_picked(
        **states: Any,  # noqa: ANN401 -- validated on the first line
    ) -> tuple[Any, ...]:
        add = _AddPicked.model_validate(states)
        if not add.clicks or not add.runs:
            raise PreventUpdate
        return _add(add.runs, add.page_json, add.paging_json)
