"""The add/edit-chart modal offers the experiment's real columns and previews exactly what a panel would show."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard.conftest import props as _to_props
from dlboard.models._view import ColumnKind, ParameterField, ParameterFieldType
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._pages._experiment import _chart_editor_modal as editor
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._chart_editor_modal import (
    _param_field_input,
    build_chart_param_form,
    render_chart_preview,
)
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog
from dlboard.serve._pages._experiment._page_model import EXCLUDED_RUNS_KEY

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


def _log_a_run(store: SQLLiteStore, experiment_id: int) -> models.Run:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5, "acc": 0.9},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    return run


def _props_by_field(fields: list[Any]) -> dict[str, dict[str, Any]]:
    return {_to_props(f)["id"]["field"]: _to_props(f) for f in fields}


def test_build_chart_param_form_offers_the_experiments_columns(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _log_a_run(store, experiment_id)

    line = _props_by_field(build_chart_param_form("line", None, store=store, experiment_id=experiment_id))
    table = _props_by_field(build_chart_param_form("table", None, store=store, experiment_id=experiment_id))

    assert set(line["column"]["data"]) >= {"loss", "acc"}
    assert "step" in line["x_axis"]["data"]
    assert set(table["metrics"]["data"]) >= {"loss", "acc"}
    assert table["hparams"]["data"] == ["lr"]


def test_render_chart_preview_leaves_out_excluded_runs_like_the_real_panel(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """It used to render from the whole experiment, so it showed runs the panel itself hides."""
    sign_in_as("alice")
    kept, excluded = _log_a_run(store, experiment_id), _log_a_run(store, experiment_id)
    # Excluding a run branches into a view of alice's own (see `save_page`) -- preview from that
    # same view, not the (untouched) shared page, to see the exclusion it just made.
    view = repo.persist_settings(store, repo.PageRef(experiment_id, None), {EXCLUDED_RUNS_KEY: [excluded.id]})

    field_ids = [{"type": "chart-param", "field": "column"}, {"type": "chart-param", "field": "x_axis"}]
    preview, error = render_chart_preview(
        "line",
        ["loss", "step"],
        [None, None],
        field_ids,
        [],
        [],
        store=store,
        ref=repo.PageRef(experiment_id, view.id),
    )

    assert error == ""
    _title, plot = _to_props(preview)["children"]
    assert [s["name"] for s in _to_props(plot)["series"]] == [str(kept.id)]


def test_merge_chart_param_values_prefers_value_over_checked() -> None:
    values = ["loss", None]
    checked = [None, True]
    field_ids = [{"field": "column"}, {"field": "sample"}]

    assert editor.merge_chart_param_values(values, checked, field_ids, {}) == {
        "column": "loss",
        "sample": True,
    }


def test_merge_chart_param_values_takes_a_tables_rows_from_its_data() -> None:
    entries = {"took": {"time_unit": "h"}}
    fields = {
        "tag_display": ParameterField(name="tag_display", type=ParameterFieldType.TABLE, required=False)
    }

    merged = editor.merge_chart_param_values(
        [], [], [], fields, table_ids=[{"field": "tag_display"}], table_values=[entries]
    )

    assert merged == {"tag_display": entries}


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

    assert editor.merge_chart_param_values(values, checked, field_ids, fields) == {"column": None}


# ---- build_validated_chart_instance ----


def test_build_validated_chart_instance_succeeds_for_valid_parameters() -> None:
    chart = editor.build_validated_chart_instance("line", {"column": "loss", "x_axis": "step"})
    assert chart.chart_type == "line"


@pytest.mark.parametrize(
    "parameters",
    [
        # Regression: a bare `ChartInstance(...)` never validates its `parameters` (untyped dict), so a
        # required field left `None` after `editor.merge_chart_param_values` used to sail through and only
        # blow up later, unguarded, from `hint_required_columns`/`render` -- with no error in the UI.
        pytest.param({"column": None, "x_axis": "step"}, id="cleared-required-field"),
        # A cleared NumberInput isn't guaranteed to report `None`; `""` isn't dropped by
        # `merge_chart_param_values` (only `None` is), so it must still fail validation here.
        pytest.param({"column": "loss", "x_axis": "step", "width": ""}, id="bad-optional-field-value"),
    ],
)
def test_build_validated_chart_instance_rejects_parameters_that_would_break_later(
    parameters: dict[str, Any],
) -> None:
    """Raising here, where the add/edit-chart callback can catch it, lets it show "Invalid parameters: ..."."""
    with pytest.raises(ValidationError):
        editor.build_validated_chart_instance("line", parameters)


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


@pytest.mark.parametrize(
    ("required", "has_clear_button"),
    [
        # An optional numeric field (e.g. width) gets a visible clear button -- backspacing to empty
        # already works, but isn't discoverable on its own.
        pytest.param(False, True, id="optional-number"),
        pytest.param(True, False, id="required-number"),
    ],
)
def test_param_field_input_number_gets_a_clear_button_only_when_optional(
    required: bool, *, has_clear_button: bool
) -> None:
    field = ParameterField(name="width", type=ParameterFieldType.INT, required=required, default=None)
    component = _param_field_input(field.name, field, ColumnCatalog())
    props = cast("Any", component).to_plotly_json()["props"]  # pyrefly: ignore [unknown-variable-type]
    if has_clear_button:
        assert props["rightSection"] is not None
        assert props["rightSectionPointerEvents"] == "all"
    else:
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
    # dash-mantine-components ships no py.typed marker, so pyrefly can't see this attr.
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
    props = cast("Any", component).to_plotly_json()["props"]  # pyrefly: ignore [unknown-variable-type]
    assert props["data"] == ["number", "category"]
    assert props["value"] == "category"
