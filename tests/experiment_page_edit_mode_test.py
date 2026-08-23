# pyright: reportPrivateUsage=false
"""Regression tests for edit-mode state surviving a panel/chart re-render.

Adding a panel or chart (or changing run selection) while in edit mode used to
silently drop back out of edit mode and wipe the cached full-dataframe store,
because `accordion_view` hardcoded `checked=False` and emitted fresh, empty
`Store`s on every re-render. These tests pin the fix: `edit_mode`,
`full_df_json`, and `column_kinds` must round-trip through a re-render.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from dltrack import models
from dltrack.models._view import ChartInstance, ColumnKind, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.plugins.pages.simple_experiment_page import (
    COLUMN_KINDS_STORE_ID,
    EDIT_MODE_ID,
    FULL_DF_STORE_ID,
    NEW_PANEL_CONTROLS_ID,
    BasicExperimentPage,
    _chart_controls_group_id,
    _open_chart_button_id,
    _persist_settings_and_rerender,
    _render_panel_charts,
    _render_panel_content,
    accordion_view,
)

if TYPE_CHECKING:
    from pathlib import Path

_TS = datetime(2026, 1, 1, tzinfo=UTC)

LineChart.register(allow_override=True)


@pytest.fixture
def store(tmp_path: Path) -> SQLLiteStore:
    return SQLLiteStore(tmp_path / "test.sqlite")


@pytest.fixture
def experiment_id(store: SQLLiteStore) -> int:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    return experiment.id


def _to_props(component: object) -> dict[str, Any]:
    return cast("Any", component).to_plotly_json()["props"]


def _find_props(component: Any, target_id: object) -> dict[str, Any] | None:  # noqa: ANN401
    """Depth-first search a dash component tree for the props of a node with `target_id`.

    `component` is typed `Any`: dash-mantine-components ships no py.typed marker, so its
    component tree (and dash's own `.children`) is Unknown to pyright regardless.
    """
    if isinstance(component, list):
        for item in cast("list[Any]", component):
            found = _find_props(item, target_id)
            if found is not None:
                return found
        return None
    if not hasattr(component, "to_plotly_json"):
        return None
    props = _to_props(component)
    if props.get("id") == target_id:
        return props
    children = props.get("children")
    return _find_props(children, target_id) if children is not None else None


def _panel_with_chart() -> PanelInstance[Any, Any]:
    return PanelInstance[Any, Any](
        name="p",
        charts=[ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})],
    )


# ---- per-chart edit/delete controls: hidden (not just disabled) outside edit mode ----


@pytest.mark.parametrize("edit_mode", [False, True])
def test_render_panel_charts_controls_visibility_matches_edit_mode(*, edit_mode: bool) -> None:
    import pandas as pd

    panel = _panel_with_chart()
    df = pd.DataFrame({"run_id": [1], "step": [0], "loss": [0.5]})

    [stack] = _render_panel_charts(panel, df, edit_mode=edit_mode)
    controls = _find_props(stack, _chart_controls_group_id("p", 0))
    assert controls is not None

    expected_style = {} if edit_mode else {"display": "none"}
    assert controls["style"] == expected_style

    [edit_icon, delete_icon] = controls["children"]
    assert _to_props(edit_icon)["disabled"] is not edit_mode
    assert _to_props(delete_icon)["disabled"] is not edit_mode


def test_render_panel_content_add_button_visibility_matches_edit_mode(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5}, step=0, experiment_id=experiment_id, run_id=run.id, timestamp_utc=_TS
            )
        ]
    )
    panel = _panel_with_chart()

    off = _find_props(
        _render_panel_content(store, experiment_id, panel, {}, edit_mode=False), _open_chart_button_id("p")
    )
    on = _find_props(
        _render_panel_content(store, experiment_id, panel, {}, edit_mode=True), _open_chart_button_id("p")
    )

    assert off is not None
    assert off["disabled"] is True
    assert off["style"] == {"display": "none"}

    assert on is not None
    assert on["disabled"] is False
    assert on["style"] == {}


# ---- accordion_view: edit_mode / cached dataframe must round-trip, not reset ----


def test_accordion_view_defaults_to_edit_mode_off(store: SQLLiteStore, experiment_id: int) -> None:
    container = accordion_view(store, experiment_id)

    switch = _find_props(cast("Any", container).children, EDIT_MODE_ID)
    assert switch is not None
    assert switch["checked"] is False

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store.get("data") is None


def test_accordion_view_preserves_edit_mode_and_cached_dataframe(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = accordion_view(
        store,
        experiment_id,
        edit_mode=True,
        full_df_json='{"cached": true}',
        column_kinds={"loss": ColumnKind.METRIC},
    )

    switch = _find_props(cast("Any", container).children, EDIT_MODE_ID)
    assert switch is not None
    assert switch["checked"] is True

    new_panel_controls = _find_props(cast("Any", container).children, NEW_PANEL_CONTROLS_ID)
    assert new_panel_controls is not None
    assert new_panel_controls["opened"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'

    column_kinds_store = _find_props(cast("Any", container).children, COLUMN_KINDS_STORE_ID)
    assert column_kinds_store is not None
    assert column_kinds_store["data"] == {"loss": ColumnKind.METRIC}


def test_persist_settings_and_rerender_does_not_reset_edit_mode(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Regression: changing run selection (or any page_settings update) while editing must not
    kick the user back out of edit mode or drop the cached dataframe used for chart previews.
    """
    store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    _page, container = _persist_settings_and_rerender(
        store,
        experiment_id,
        {"open_panel": []},
        edit_mode=True,
        full_df_json='{"cached": true}',
        column_kinds={"loss": ColumnKind.METRIC},
    )

    switch = _find_props(cast("Any", container).children, EDIT_MODE_ID)
    assert switch is not None
    assert switch["checked"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'
