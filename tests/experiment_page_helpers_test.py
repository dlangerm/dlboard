# pyright: reportPrivateUsage=false
"""Tests for the pure (non-Dash) helpers in the basic experiment page."""

from __future__ import annotations

from typing import Any, cast

import dash_mantine_components as dmc
import pandas as pd
import pytest

from dltrack.models import HyperParams, NewHyperParams
from dltrack.models._view import ChartInstance, ColumnKind, PanelInstance, ParameterField, ParameterFieldType
from dltrack.plugins.pages.simple_experiment_page import (
    _build_hparam_rows,
    _merge_chart_param_values,
    _param_field_input,
    _upsert_chart,
)


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

    result = _upsert_chart(panel, "p", index, new_chart)

    assert result.charts[index].parameters == {"column": "new"}
    untouched = [i for i in range(3) if i != index]
    assert [result.charts[i].parameters["column"] for i in untouched] == [f"m{i}" for i in untouched]


def test_upsert_chart_appends_when_index_is_none() -> None:
    panel = _panel("p", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    result = _upsert_chart(panel, "p", None, new_chart)

    assert len(result.charts) == 2
    assert result.charts[-1].parameters == {"column": "new"}


def test_upsert_chart_ignores_other_panels() -> None:
    panel = _panel("other", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    assert _upsert_chart(panel, "p", 0, new_chart) is panel


def test_merge_chart_param_values_prefers_value_over_checked() -> None:
    values = ["loss", None]
    checked = [None, True]
    field_ids = [{"field": "column"}, {"field": "sample"}]

    assert _merge_chart_param_values(values, checked, field_ids, {}) == {"column": "loss", "sample": True}


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

    assert _merge_chart_param_values(values, checked, field_ids, fields) == {"column": None}


def test_build_hparam_rows_merges_hparams_and_last_step_metrics() -> None:
    hydrated = [
        HyperParams(
            id=1,
            run_id=1,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(1, 1, {"lr": 0.1}).raw_hparams,
        ),
        HyperParams(
            id=2,
            run_id=2,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(2, 1, {"lr": 0.2}).raw_hparams,
        ),
    ]
    last_step_metrics = {1: {"loss": 0.5}}

    rows = _build_hparam_rows(hydrated, last_step_metrics)

    assert rows == [
        {"run_id": 1, "lr": 0.1, "loss": 0.5},
        {"run_id": 2, "lr": 0.2},
    ]


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
    columns_by_kind = {ColumnKind.METRIC: ["loss", "acc"]}
    component = _param_field_input(field.name, field, columns_by_kind)
    assert isinstance(component, expected_type)


def test_param_field_input_offers_grouping_kind_as_a_select() -> None:
    """A bar chart's x_axis (ColumnKind.GROUPING) must be pickable, not a blind free-text field --
    otherwise a user has no way to discover a groupable column like `run_id`."""
    field = ParameterField(
        name="x_axis", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.GROUPING
    )
    component = _param_field_input(field.name, field, {ColumnKind.GROUPING: ["loss", "lr", "run_id"]})
    assert isinstance(component, dmc.Select)


def test_param_field_input_falls_back_to_text_when_no_columns_of_kind() -> None:
    field = ParameterField(
        name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.ARTIFACT
    )
    component = _param_field_input(field.name, field, {ColumnKind.METRIC: ["loss"]})
    assert isinstance(component, dmc.TextInput)


def test_param_field_input_uses_override_over_default() -> None:
    field = ParameterField(name="sample", type=ParameterFieldType.BOOL, required=False, default=True)
    component = _param_field_input(field.name, field, {}, override=False)
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
    component = _param_field_input(field.name, field, {}, override="category")

    assert isinstance(component, dmc.Select)
    props = cast("Any", component).to_plotly_json()["props"]
    assert props["data"] == ["number", "category"]
    assert props["value"] == "category"
