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
from dltrack.models import constants
from dltrack.models._view import ChartInstance, ColumnKind, PanelInstance
from dltrack.plugins.charts.line_chart import LineChart
from dltrack.plugins.data_stores.sqlite import SQLLiteStore
from dltrack.plugins.pages.simple_experiment_page import (
    AUTO_POPULATE_BUTTON_ID,
    COLUMN_KINDS_STORE_ID,
    EDIT_DRAWER_ID,
    EDIT_MODE_ID,
    FULL_DF_STORE_ID,
    SUGGEST_CHARTS_BUTTON_ID,
    BasicExperimentPage,
    _add_chart_to_panel_by_name,
    _chart_controls_group_id,
    _delete_panel_button_id,
    _header_actions,
    _move_panel,
    _move_panel_button_id,
    _open_chart_button_id,
    _panel_management_list,
    _persist_settings_and_rerender,
    _rename_panel_button_id,
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


# ---- "Manage panels" list: rename/delete/reorder controls work without opening a panel ----
# Deliberately a separate list outside the accordion, not layered onto AccordionControl (which
# is a native full-width Mantine button and clips/fights any sibling or overlay content placed
# in its row) — see `_panel_management_row`'s docstring.


def test_panel_management_list_empty_when_no_panels() -> None:
    from dash import html

    assert isinstance(_panel_management_list([]), html.Div)


def test_panel_management_list_has_a_row_per_panel_with_working_controls() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("train", "val")]

    rendered = _panel_management_list(panels)

    # Neither button sets `disabled` explicitly: they're plain, always-enabled buttons, hidden as
    # a unit by the containing edit-mode Collapse rather than individually disabled.
    delete_button = _find_props(rendered, _delete_panel_button_id("train"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True

    rename_button = _find_props(rendered, _rename_panel_button_id("train"))
    assert rename_button is not None
    assert rename_button.get("disabled") is not True


def test_panel_management_list_move_buttons_disabled_at_boundaries() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]

    rendered = _panel_management_list(panels)

    assert _find_props(rendered, _move_panel_button_id("a", "up"))["disabled"] is True  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(rendered, _move_panel_button_id("a", "down"))["disabled"] is False  # pyright: ignore[reportOptionalSubscript]

    assert _find_props(rendered, _move_panel_button_id("b", "up"))["disabled"] is False  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(rendered, _move_panel_button_id("b", "down"))["disabled"] is False  # pyright: ignore[reportOptionalSubscript]

    assert _find_props(rendered, _move_panel_button_id("c", "up"))["disabled"] is False  # pyright: ignore[reportOptionalSubscript]
    assert _find_props(rendered, _move_panel_button_id("c", "down"))["disabled"] is True  # pyright: ignore[reportOptionalSubscript]


# ---- _move_panel ----


def test_move_panel_up_swaps_with_previous() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = _move_panel(panels, "b", "up")
    assert [p.name for p in result] == ["b", "a", "c"]


def test_move_panel_down_swaps_with_next() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = _move_panel(panels, "b", "down")
    assert [p.name for p in result] == ["a", "c", "b"]


def test_move_panel_at_boundary_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    assert [p.name for p in _move_panel(panels, "a", "up")] == ["a", "b", "c"]
    assert [p.name for p in _move_panel(panels, "c", "down")] == ["a", "b", "c"]


def test_move_panel_unknown_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert _move_panel(panels, "missing", "up") == panels


# ---- header actions: Runs + Edit switch, rendered once and never regenerated ----


def test_header_actions_has_runs_button_and_edit_switch_defaulted_off() -> None:
    actions = _header_actions()

    runs_button = _find_props(actions, constants.HPARAM_DRAWER_TOGGLE_ID)
    assert runs_button is not None

    switch = _find_props(actions, EDIT_MODE_ID)
    assert switch is not None
    assert switch["checked"] is False


# ---- accordion_view: edit_mode / cached dataframe must round-trip, not reset ----


def test_accordion_view_defaults_to_edit_drawer_closed(store: SQLLiteStore, experiment_id: int) -> None:
    container = accordion_view(store, experiment_id)

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is False

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store.get("data") is None


def test_accordion_view_preserves_edit_drawer_state_and_cached_dataframe(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = accordion_view(
        store,
        experiment_id,
        edit_mode=True,
        edit_drawer_opened=True,
        full_df_json='{"cached": true}',
        column_kinds={"loss": ColumnKind.METRIC},
    )

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'

    column_kinds_store = _find_props(cast("Any", container).children, COLUMN_KINDS_STORE_ID)
    assert column_kinds_store is not None
    assert column_kinds_store["data"] == {"loss": ColumnKind.METRIC}


def test_persist_settings_and_rerender_does_not_reset_edit_drawer_state(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Regression: changing run selection (or any page_settings update) while editing must not
    close the edit drawer or drop the cached dataframe used for chart previews.
    """
    store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    _page, container = _persist_settings_and_rerender(
        store,
        experiment_id,
        {"open_panel": []},
        edit_mode=True,
        edit_drawer_opened=True,
        full_df_json='{"cached": true}',
        column_kinds={"loss": ColumnKind.METRIC},
    )

    drawer = _find_props(cast("Any", container).children, EDIT_DRAWER_ID)
    assert drawer is not None
    assert drawer["opened"] is True

    full_df_store = _find_props(cast("Any", container).children, FULL_DF_STORE_ID)
    assert full_df_store is not None
    assert full_df_store["data"] == '{"cached": true}'


# ---- toolbar: auto-generate charts (empty view) vs suggest charts (non-empty view) ----


def test_accordion_view_shows_auto_populate_when_view_is_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    container = accordion_view(store, experiment_id, edit_mode=True)

    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is None


def test_accordion_view_shows_suggest_charts_when_view_is_not_empty(
    store: SQLLiteStore, experiment_id: int
) -> None:
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id, edit_mode=True)

    assert _find_props(cast("Any", container).children, SUGGEST_CHARTS_BUTTON_ID) is not None
    assert _find_props(cast("Any", container).children, AUTO_POPULATE_BUTTON_ID) is None


def test_accordion_view_manage_panels_list_reachable_without_opening_a_panel(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The whole point: delete/rename/reorder must be usable without opening (fetching) the panel."""
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="p")]}))

    container = accordion_view(store, experiment_id, edit_mode=True)

    delete_button = _find_props(cast("Any", container).children, _delete_panel_button_id("p"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True


# ---- _add_chart_to_panel_by_name ----


def test_add_chart_to_panel_by_name_creates_panel_when_missing() -> None:
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})

    panels = _add_chart_to_panel_by_name([], "train", chart)

    assert [p.name for p in panels] == ["train"]
    assert panels[0].charts == [chart]


def test_add_chart_to_panel_by_name_appends_to_existing_panel() -> None:
    existing_chart = ChartInstance[Any, Any](
        chart_type="line", parameters={"column": "loss", "x_axis": "step"}
    )
    new_chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "acc", "x_axis": "step"})
    panels = [
        PanelInstance[Any, Any](name="train", charts=[existing_chart]),
        PanelInstance[Any, Any](name="val"),
    ]

    result = _add_chart_to_panel_by_name(panels, "train", new_chart)

    by_name = {p.name: p for p in result}
    assert by_name["train"].charts == [existing_chart, new_chart]
    assert by_name["val"].charts == []
