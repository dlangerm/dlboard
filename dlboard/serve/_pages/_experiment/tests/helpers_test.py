# pyright: reportPrivateUsage=false
"""Tests for the pure (non-Dash) helpers in the basic experiment page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.models import HyperParams, NewHyperParams, Run
from dlboard.models._view import ChartInstance, ColumnKind, PanelInstance, ParameterField, ParameterFieldType
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment import _run_comparison_table as run_table
from dlboard.serve._pages._experiment._chart_editor_modal import _param_field_input
from dlboard.serve._pages._experiment._dataframe_helpers import ColumnCatalog

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)
ImageChart.register(allow_override=True)


def _panel(name: str, n_charts: int) -> PanelInstance[pd.DataFrame, object]:
    return PanelInstance[pd.DataFrame, object](
        name=name,
        charts=[
            ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": f"m{i}"})
            for i in range(n_charts)
        ],
    )


@pytest.mark.parametrize("index", [0, 1, 2])
def test_upsert_chart_replaces_the_requested_index(index: int) -> None:
    panel = _panel("p", 3)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    result = state.upsert_chart(panel, "p", index, new_chart)

    assert result.charts[index].parameters == {"column": "new"}
    untouched = [i for i in range(3) if i != index]
    assert [result.charts[i].parameters["column"] for i in untouched] == [f"m{i}" for i in untouched]


def test_upsert_chart_appends_when_index_is_none() -> None:
    panel = _panel("p", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    result = state.upsert_chart(panel, "p", None, new_chart)

    assert len(result.charts) == 2
    assert result.charts[-1].parameters == {"column": "new"}


def test_upsert_chart_ignores_other_panels() -> None:
    panel = _panel("other", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    assert state.upsert_chart(panel, "p", 0, new_chart) is panel


def test_merge_chart_param_values_prefers_value_over_checked() -> None:
    values = ["loss", None]
    checked = [None, True]
    field_ids = [{"field": "column"}, {"field": "sample"}]

    assert state.merge_chart_param_values(values, checked, field_ids, {}) == {
        "column": "loss",
        "sample": True,
    }


def test_merge_chart_param_values_drops_cleared_optional_fields() -> None:
    """A cleared NumberInput reports `None`, which isn't a valid `int` — for a field with its own
    default, that should be dropped so validation falls back to the field's default rather than
    failing with a "not a valid integer" error (regression: clearing page_size/font_size got stuck).
    """
    values = [None, None]
    checked = [None, None]
    field_ids = [{"field": "page_size"}, {"field": "column"}]
    fields = {
        "page_size": ParameterField(
            name="page_size", type=ParameterFieldType.INT, required=False, default=20
        ),
        "column": ParameterField(name="column", type=ParameterFieldType.STR, required=True),
    }

    assert state.merge_chart_param_values(values, checked, field_ids, fields) == {"column": None}


# ---- build_validated_chart_instance ----


def test_build_validated_chart_instance_succeeds_for_valid_parameters() -> None:
    chart = state.build_validated_chart_instance("line", {"column": "loss", "x_axis": "step"})
    assert chart.chart_type == "line"


def test_build_validated_chart_instance_rejects_a_cleared_required_field() -> None:
    """Regression: a bare `ChartInstance(...)` never validates its `parameters` (untyped dict), so
    a required field left `None` after `state.merge_chart_param_values` (see above) used to sail through
    unnoticed and only blow up later, unguarded, from `hint_required_columns`/`render` -- with no
    error surfaced in the UI. This must raise here instead, where the add/edit-chart callback can
    catch it and show "Invalid parameters: ...".
    """
    with pytest.raises(ValidationError):
        state.build_validated_chart_instance("line", {"column": None, "x_axis": "step"})


def test_build_validated_chart_instance_rejects_a_bad_optional_field_value() -> None:
    """A cleared NumberInput isn't guaranteed to report `None` -- if it reports `""` instead, that's
    not dropped by `state.merge_chart_param_values` (only `None` is), so it must still fail validation
    here rather than reach `render` unguarded.
    """
    with pytest.raises(ValidationError):
        state.build_validated_chart_instance("line", {"column": "loss", "x_axis": "step", "width": ""})


def _run(run_id: int, name: str | None = None) -> Run:
    return Run(id=run_id, experiment_id=1, name=name, created_at=_TS)


_RUNNING_FIELDS = {"_status": "running", "_duration_s": None, "_status_detail": "running"}


def test_build_hparam_rows_merges_hparams_and_latest_metrics() -> None:
    runs = [_run(1), _run(2)]
    hparams_by_run = {
        1: HyperParams(
            id=1,
            run_id=1,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(1, 1, {"lr": 0.1}).raw_hparams,
        ),
        2: HyperParams(
            id=2,
            run_id=2,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(2, 1, {"lr": 0.2}).raw_hparams,
        ),
    }
    latest_metrics = {1: {"loss": 0.5}}

    rows = run_table._build_hparam_rows(runs, hparams_by_run, latest_metrics)

    assert rows == [
        {"run_id": 1, "run_name": "Run 1", **_RUNNING_FIELDS, "lr": 0.1, "loss": 0.5},
        {"run_id": 2, "run_name": "Run 2", **_RUNNING_FIELDS, "lr": 0.2},
    ]


def test_build_hparam_rows_includes_runs_with_no_logged_hyperparameters() -> None:
    """A run that hasn't called `log_hyperparams` yet must still show up, not disappear."""
    runs = [_run(1, name="baseline"), _run(2)]

    rows = run_table._build_hparam_rows(runs, hparams_by_run={}, latest_metrics={})

    assert rows == [
        {"run_id": 1, "run_name": "baseline", **_RUNNING_FIELDS},
        {"run_id": 2, "run_name": "Run 2", **_RUNNING_FIELDS},
    ]


def test_persist_settings_merges_into_page_settings_without_touching_panels(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Unlike `_persist_settings_and_rerender`, this must not need/trigger an accordion rebuild."""
    sign_in_as("alice")
    view = store.create_view(
        state.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="mine",
        ),
    )

    page = state.persist_settings(store, state.PageRef(experiment_id, view.id), {"selected": ["lr"]})

    assert page.page_settings["selected"] == ["lr"]
    assert page.id == view.id  # written in place -- alice already owns this one
    reloaded = store.get_view(state.BasicExperimentPage, view.id)
    assert reloaded is not None
    assert reloaded.page_settings["selected"] == ["lr"]


def test_persist_settings_branches_into_your_own_view_when_editing_the_shared_page(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """
    The shared page has no owner, so editing it while looking at it (`view_id=None`) can never
    write in place -- otherwise the very first edit anyone made from the shared page would change
    what everyone else sees.
    """
    sign_in_as("alice")

    page = state.persist_settings(store, state.PageRef(experiment_id, None), {"selected": ["lr"]})

    assert page.page_settings["selected"] == ["lr"]
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("alice")).id
    shared = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    assert shared.id != page.id
    assert shared.page_settings == {}


def test_persist_settings_branches_into_a_separate_view_when_editing_someone_elses(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Bob editing alice's view must never change what alice sees -- it forks a view of bob's own."""
    sign_in_as("alice")
    alices_view = store.create_view(
        state.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="alice's view",
            panels=[PanelInstance[Any, Any](name="p")],
        ),
    )

    sign_in_as("bob")
    page = state.persist_settings(store, state.PageRef(experiment_id, alices_view.id), {"selected": ["lr"]})

    assert page.id != alices_view.id
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("bob")).id
    assert page.panels == alices_view.panels  # seeded from what bob was looking at
    assert page.page_settings["selected"] == ["lr"]
    untouched = store.get_view(state.BasicExperimentPage, alices_view.id)
    assert untouched is not None
    assert untouched.page_settings == {}


def test_persist_settings_can_opt_out_of_branching_for_purely_personal_viewing_state(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Which panel/tab is open is shared, last-write-wins state -- opening one never forks a view."""
    sign_in_as("alice")
    store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)

    page = state.persist_settings(
        store, state.PageRef(experiment_id, None), {"open_panel": ["p"]}, branch_on_edit=False
    )

    assert page.owner_id is None
    shared = store.get_or_create_page(state.BasicExperimentPage, experiment_id=experiment_id)
    assert shared.page_settings["open_panel"] == ["p"]


def _log_a_metric(store: SQLLiteStore, experiment_id: int, run_id: int, key: str, step: int = 0) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={key: 1.0}, step=step, experiment_id=experiment_id, run_id=run_id, timestamp_utc=_TS
            )
        ]
    )


def test_a_metric_and_an_artifact_sharing_a_key_both_render(store: SQLLiteStore, experiment_id: int) -> None:
    """They used to be outer-merged into `img_x`/`img_y`, so a chart of either `img` raised `KeyError`."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "img")
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="i.png", run_id=run.id, experiment_id=experiment_id, step=0, ref="r://a"
            )
        ]
    )
    panel = PanelInstance[pd.DataFrame, object](
        name="p",
        charts=[
            ChartInstance[pd.DataFrame, object](
                chart_type="line", parameters={"column": "img", "x_axis": "step"}
            ),
            ChartInstance[pd.DataFrame, object](chart_type="image", parameters={"key": "img"}),
        ],
    )

    df = state.fetch_panel_dataframe(store, experiment_id, panel, {})

    for chart in panel.charts:
        chart.render(df)


def test_load_hparam_view_data_shows_each_selected_metrics_latest_value(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Used to read only the run's highest-step row, blanking any metric not logged at that exact step."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "val/acc", step=5)
    _log_a_metric(store, experiment_id, run.id, "train/loss", step=9)

    _hk, _mk, rows = run_table._load_hparam_view_data(
        store, experiment_id, [], selected_metrics={"val/acc", "train/loss"}, runs=[run]
    )

    assert rows == [
        {"run_id": run.id, "run_name": run.name, **_RUNNING_FIELDS, "val/acc": 1.0, "train/loss": 1.0}
    ]


def test_load_hparam_view_data_reports_available_metric_keys_without_fetching_values(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """`metric_keys` (for the Columns picker) must come from the cheap key listing, not a full
    fetch -- rows stay metric-free until a column is actually selected."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "loss")

    hparam_keys, metric_keys, rows = run_table._load_hparam_view_data(
        store, experiment_id, [], selected_metrics=set(), runs=[run]
    )

    assert metric_keys == ["loss"]
    assert hparam_keys == []
    assert rows == [{"run_id": run.id, "run_name": run.name, **_RUNNING_FIELDS}]


def test_load_hparam_view_data_includes_values_for_selected_metrics(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "loss")

    _hk, _mk, rows = run_table._load_hparam_view_data(
        store, experiment_id, [], selected_metrics={"loss"}, runs=[run]
    )

    assert rows == [{"run_id": run.id, "run_name": run.name, **_RUNNING_FIELDS, "loss": 1.0}]


@pytest.mark.parametrize(
    ("field", "expected_type"),
    [
        (
            ParameterField(name="sample", type=ParameterFieldType.BOOL, required=False, default=True),
            dmc.Switch,
        ),
        (
            ParameterField(name="height", type=ParameterFieldType.INT, required=False, default=300),
            dmc.NumberInput,
        ),
        (
            ParameterField(
                name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.METRIC
            ),
            dmc.Select,
        ),
        (ParameterField(name="name", type=ParameterFieldType.STR, required=False), dmc.TextInput),
    ],
)
def test_param_field_input_picks_widget_by_field_type(field: ParameterField, expected_type: type) -> None:
    catalog = ColumnCatalog(metrics=("loss", "acc"))
    component = _param_field_input(field.name, field, catalog)
    assert isinstance(component, expected_type)


def test_param_field_input_optional_number_gets_a_clear_button() -> None:
    """An optional numeric field (e.g. width) gets a visible clear button -- backspacing to empty
    already works, but isn't discoverable on its own."""
    field = ParameterField(name="width", type=ParameterFieldType.INT, required=False, default=None)
    component = _param_field_input(field.name, field, ColumnCatalog())
    props = cast("Any", component).to_plotly_json()["props"]
    assert props["rightSection"] is not None
    assert props["rightSectionPointerEvents"] == "all"


def test_param_field_input_required_number_has_no_clear_button() -> None:
    field = ParameterField(name="page_size", type=ParameterFieldType.INT, required=True)
    component = _param_field_input(field.name, field, ColumnCatalog())
    props = cast("Any", component).to_plotly_json()["props"]
    assert props.get("rightSection") is None


def test_param_field_input_offers_grouping_kind_as_a_select() -> None:
    """A bar chart's x_axis (ColumnKind.GROUPING) must be pickable, not a blind free-text field --
    otherwise a user has no way to discover a groupable column like `run_id`."""
    field = ParameterField(
        name="x_axis", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.GROUPING
    )
    component = _param_field_input(field.name, field, ColumnCatalog(metrics=("loss",), hparams=("lr",)))
    assert isinstance(component, dmc.Select)


def test_param_field_input_falls_back_to_text_when_no_columns_of_kind() -> None:
    field = ParameterField(
        name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.ARTIFACT
    )
    component = _param_field_input(field.name, field, ColumnCatalog(metrics=("loss",)))
    assert isinstance(component, dmc.TextInput)


def test_param_field_input_uses_override_over_default() -> None:
    field = ParameterField(name="sample", type=ParameterFieldType.BOOL, required=False, default=True)
    component = _param_field_input(field.name, field, ColumnCatalog(), override=False)
    # dash-mantine-components ships no py.typed marker, so pyright can't see this attr.
    assert cast("Any", component).to_plotly_json()["props"]["checked"] is False


def test_param_field_input_renders_fixed_choices_as_a_select() -> None:
    """A `Literal[...]`-typed field (e.g. line chart's `x_axis_type`) gets a dropdown of its fixed
    choices, taking priority over the column-kind-derived options path.
    """
    field = ParameterField(
        name="x_axis_type",
        type=ParameterFieldType.STR,
        required=False,
        default="number",
        choices=("number", "category"),
    )
    component = _param_field_input(field.name, field, ColumnCatalog(), override="category")

    assert isinstance(component, dmc.Select)
    props = cast("Any", component).to_plotly_json()["props"]
    assert props["data"] == ["number", "category"]
    assert props["value"] == "category"


@pytest.mark.parametrize(
    ("status", "ran_for_s", "expected"),
    [
        pytest.param(
            models.RunStatus.FINISHED, 203, ("finished", 203, "finished after 3m 23s"), id="finished"
        ),
        pytest.param(models.RunStatus.FAILED, 3725, ("failed", 3725, "failed after 1h 2m"), id="failed"),
        pytest.param(models.RunStatus.FINISHED, 42, ("finished", 42, "finished after 42s"), id="seconds"),
        pytest.param(models.RunStatus.RUNNING, None, ("running", None, "running"), id="running"),
        pytest.param(None, None, (None, None, ""), id="a-run-stored-before-status-existed"),
    ],
)
def test_build_hparam_rows_shows_how_a_run_ended_and_how_long_it_took(
    status: models.RunStatus | None, ran_for_s: int | None, expected: tuple[str | None, int | None, str]
) -> None:
    ended_at = None if ran_for_s is None else _TS + timedelta(seconds=ran_for_s)
    run = _run(1).model_copy(update={"status": status, "ended_at": ended_at})

    (row,) = run_table._build_hparam_rows([run], hparams_by_run={}, latest_metrics={})

    assert (row["_status"], row["_duration_s"], row["_status_detail"]) == expected


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m 0s"),
        (203, "3m 23s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3725, "1h 2m"),
    ],
)
def test_a_duration_reads_as_seconds_minutes_or_hours(seconds: int, text: str) -> None:
    assert run_table._duration_text(seconds) == text


def test_the_runs_table_redraws_when_a_run_finishes() -> None:
    """Its signature is what gates a redraw, so a status or end time has to be part of it."""
    running = _run(1)
    finished = running.model_copy(
        update={"status": models.RunStatus.FINISHED, "ended_at": _TS + timedelta(minutes=1)}
    )

    def signature(run: Run) -> list[Any]:
        return run_table._render_signature(1, [], [], [], [run])

    assert signature(running) != signature(finished)
    assert signature(finished) == signature(finished.model_copy())
