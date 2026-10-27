"""Tests for the panel accordion header (hover controls) and the panel-mutation helpers.

Sync/layout/rename/drag/delete all render as hover-revealed controls on each panel's own accordion
header (`panel_header`/`panel_header_controls`) -- true DOM siblings of `AccordionControl` there,
not nested inside its native `<button>`. There's no separate "Manage panels" drawer/list anymore.
Reordering is drag-and-drop (`_experiment_page_dragdrop.js`), not up/down buttons.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, cast

import dash_mantine_components as dmc
import pandas as pd
import pytest
from dash._utils import to_json
from pydantic import ValidationError

from dlboard.conftest import OneFrame
from dlboard.conftest import find_props as _find_props
from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.plugins.charts._chart_title import titled
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_view as panel_view

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

LineChart.register(allow_override=True)

# ---- panel header: hover-revealed sync/layout/rename/drag/delete, siblings of AccordionControl ----


def test_panel_header_contains_accordion_control_and_hover_controls() -> None:
    panel = PanelInstance[Any, Any](name="train")

    rendered = panel_view.panel_header(panel)

    assert _find_props(rendered, panel_view.DELETE_PANEL_BUTTON.of("train")) is not None
    assert _find_props(rendered, panel_view.PANEL_DRAG_HANDLE.of("train")) is not None
    assert _find_props(rendered, panel_view.RENAME_PANEL_BUTTON.of("train")) is not None
    assert _find_props(rendered, panel_view.PANEL_SYNC_SWITCH.of("train")) is not None
    assert _find_props(rendered, panel_view.PANEL_LAYOUT_CONTROL.of("train")) is not None


def test_panel_header_controls_delete_is_always_enabled() -> None:
    controls = panel_view.panel_header_controls(PanelInstance[Any, Any](name="train"))

    delete_button = _find_props(controls, panel_view.DELETE_PANEL_BUTTON.of("train"))
    assert delete_button is not None
    assert delete_button.get("disabled") is not True


def test_panel_header_controls_drag_handle_carries_the_panel_name() -> None:
    controls = panel_view.panel_header_controls(PanelInstance[Any, Any](name="train"))

    handle = _find_props(controls, panel_view.PANEL_DRAG_HANDLE.of("train"))
    assert handle is not None
    assert handle["draggable"] == "true"
    assert handle["data-panel-name"] == "train"


def test_panel_header_controls_shows_sync_and_layout_state() -> None:
    synced = panel_view.panel_header_controls(PanelInstance[Any, Any](name="p", sync=True, layout="grid"))
    unsynced = panel_view.panel_header_controls(
        PanelInstance[Any, Any](name="p", sync=False, layout="packed")
    )

    synced_switch = _find_props(synced, panel_view.PANEL_SYNC_SWITCH.of("p"))
    synced_layout = _find_props(synced, panel_view.PANEL_LAYOUT_CONTROL.of("p"))
    unsynced_switch = _find_props(unsynced, panel_view.PANEL_SYNC_SWITCH.of("p"))
    unsynced_layout = _find_props(unsynced, panel_view.PANEL_LAYOUT_CONTROL.of("p"))
    assert synced_switch is not None
    assert synced_switch["checked"] is True
    assert synced_layout is not None
    assert synced_layout["value"] == "grid"
    assert unsynced_switch is not None
    assert unsynced_switch["checked"] is False
    assert unsynced_layout is not None
    assert unsynced_layout["value"] == "packed"


# ---- reorder_panel ----


@pytest.mark.parametrize(
    ("moved", "target", "after", "expected"),
    [
        pytest.param("c", "a", False, ["c", "a", "b"], id="before-target"),
        pytest.param("a", "b", True, ["b", "a", "c"], id="after-target"),
    ],
)
def test_reorder_panel_moves_it_before_or_after_the_target(
    moved: str, target: str, *, after: bool, expected: list[str]
) -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    result = page_model.reorder_panel(panels, moved, target, after=after)
    assert [p.name for p in result] == expected


def test_reorder_panel_same_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b", "c")]
    assert page_model.reorder_panel(panels, "a", "a", after=True) == panels


def test_reorder_panel_unknown_panel_is_a_noop() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert page_model.reorder_panel(panels, "missing", "a", after=True) == panels
    assert page_model.reorder_panel(panels, "a", "missing", after=True) == panels


# ---- panel sync: switch + set_panel_sync + _apply_panel_sync ----


def test_set_panel_sync_updates_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = page_model.set_panel_sync(panels, "a", sync=False)

    by_name = {p.name: p.sync for p in result}
    assert by_name == {"a": False, "b": True}


def test_apply_panel_sync_drops_sync_id_when_disabled() -> None:
    chart = dmc.LineChart(
        data=[], dataKey="step", series=[], lineChartProps={"syncId": "step", "syncMethod": "value"}
    )

    result = chart_view._apply_panel_sync(chart, panel_name="train", sync=False)

    assert "syncId" not in result.lineChartProps  # pyrefly: ignore [missing-attribute]
    assert result.lineChartProps["syncMethod"] == "value"  # pyrefly: ignore [bad-index]


# ---- panel layout: segmented control, grid column count, update_panel ----


def test_update_panel_changes_only_the_named_panel() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]

    result = page_model.update_panel(panels, "a", {"layout": "grid", "grid_columns": 2})

    assert {p.name: (p.layout, p.grid_columns) for p in result} == {"a": ("grid", 2), "b": ("packed", 3)}


@pytest.mark.parametrize("columns", [0, 7, -1])
def test_a_grid_column_count_outside_the_supported_range_is_rejected(columns: int) -> None:
    with pytest.raises(ValidationError):
        PanelInstance[Any, Any](name="a", grid_columns=columns)


def test_a_grid_panel_renders_with_its_own_column_count() -> None:
    panel = PanelInstance[Any, Any](name="a", layout="grid", grid_columns=2)

    rendered = panel_view.render_panel_grid(panel, OneFrame(pd.DataFrame()))

    assert isinstance(rendered, dmc.SimpleGrid)
    assert rendered.cols == 2  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize(("layout", "shows_columns"), [("grid", True), ("packed", False)])
def test_the_grid_column_input_is_only_offered_for_a_grid_panel(
    layout: Literal["packed", "grid"], *, shows_columns: bool
) -> None:
    controls = panel_view.panel_header_controls(
        PanelInstance[Any, Any](name="a", layout=layout, grid_columns=4)
    )

    columns = _find_props(controls, panel_view.PANEL_GRID_COLUMNS.of("a"))
    assert (columns is not None) is shows_columns
    if columns is not None:
        assert columns["value"] == 4


def test_apply_panel_sync_scopes_sync_id_to_the_panel_when_enabled() -> None:
    chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    result = chart_view._apply_panel_sync(chart, panel_name="train", sync=True)

    assert result.lineChartProps["syncId"] == "train:step"  # pyrefly: ignore [missing-attribute]


def test_apply_panel_sync_scoping_keeps_different_panels_apart() -> None:
    """The whole point: two panels sharing the same x-axis name (e.g. "step") must not sync."""
    train_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})
    val_chart = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    train_result = chart_view._apply_panel_sync(train_chart, panel_name="train", sync=True)
    val_result = chart_view._apply_panel_sync(val_chart, panel_name="val", sync=True)

    # pyrefly: ignore [missing-attribute]
    assert train_result.lineChartProps["syncId"] != val_result.lineChartProps["syncId"]


def test_apply_panel_sync_ignores_non_line_charts() -> None:
    other = dmc.Text("not a line chart")

    assert chart_view._apply_panel_sync(other, panel_name="train", sync=False) is other


# ---- reorder_chart ----


def _panel_with_charts(*names: str) -> PanelInstance[Any, Any]:
    charts = [
        ChartInstance[Any, Any](chart_type="line", parameters={"column": n, "x_axis": "step"}) for n in names
    ]
    return PanelInstance[Any, Any](name="p", charts=charts)


@pytest.mark.parametrize(
    ("index", "target", "after", "expected"),
    [
        pytest.param(0, 2, False, ["b", "a", "c", "d"], id="before-target"),
        pytest.param(3, 1, True, ["a", "b", "d", "c"], id="after-target"),
    ],
)
def test_reorder_chart_moves_it_before_or_after_the_target(
    index: int, target: int, *, after: bool, expected: list[str]
) -> None:
    panels = [_panel_with_charts("a", "b", "c", "d")]
    result = page_model.reorder_chart(panels, "p", index, target, after=after)
    assert [c.parameters["column"] for c in result[0].charts] == expected


def test_reorder_chart_same_index_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    result = page_model.reorder_chart(panels, "p", 1, 1, after=True)
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b", "c"]


def test_reorder_chart_out_of_bounds_index_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b", "c")]
    assert [
        c.parameters["column"] for c in page_model.reorder_chart(panels, "p", 5, 0, after=False)[0].charts
    ] == [
        "a",
        "b",
        "c",
    ]
    assert [
        c.parameters["column"] for c in page_model.reorder_chart(panels, "p", 0, 5, after=False)[0].charts
    ] == [
        "a",
        "b",
        "c",
    ]


def test_reorder_chart_unknown_panel_is_a_noop() -> None:
    panels = [_panel_with_charts("a", "b")]
    result = page_model.reorder_chart(panels, "missing", 0, 1, after=True)
    assert [c.parameters["column"] for c in result[0].charts] == ["a", "b"]


# ---- add_chart_to_panel_by_name ----


def test_add_chart_to_panel_by_name_creates_panel_when_missing() -> None:
    chart = ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})

    panels = page_model.add_chart_to_panel_by_name([], "train", chart)

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

    result = page_model.add_chart_to_panel_by_name(panels, "train", new_chart)

    by_name = {p.name: p for p in result}
    assert by_name["train"].charts == [existing_chart, new_chart]
    assert by_name["val"].charts == []


# ---- panel tabs: grouping panels under `PanelInstance.tab` ----


def test_render_with_no_tabbed_panels_shows_no_tabs_chrome(store: SQLLiteStore, experiment_id: int) -> None:
    """The common case (nobody has tabbed a panel) must look exactly like a plain accordion."""
    page = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    page = store.update_page(page.model_copy(update={"panels": [PanelInstance[Any, Any](name="a")]}))

    typed = cast("page_model.BasicExperimentPage", page)
    rendered = panel_view._page_view({}, typed, panel_view.FIRST_PAGE)
    assert not isinstance(rendered, dmc.Tabs)
    assert _find_props(rendered, panel_view.PANEL_ACCORDION.of("")) is not None


def test_render_groups_panels_into_tabs_by_the_tab_field(store: SQLLiteStore, experiment_id: int) -> None:
    page = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    page = store.update_page(
        page.model_copy(
            update={
                "panels": [
                    PanelInstance[Any, Any](name="a"),
                    PanelInstance[Any, Any](name="b", tab="Images"),
                    PanelInstance[Any, Any](name="c", tab="Images"),
                ]
            }
        )
    )

    typed = cast("page_model.BasicExperimentPage", page)
    rendered = panel_view._page_view({}, typed, panel_view.FIRST_PAGE)
    assert isinstance(rendered, dmc.Tabs)
    tabs_row = rendered.children[0]  # pyrefly: ignore [unsupported-operation]
    tabs_and_buttons = tabs_row.children[0]
    tabs_list = tabs_and_buttons.children[0]
    tabs = tabs_list.children
    tab_labels = {tab.children for tab in tabs}
    assert tab_labels == {"General", "Images"}
    # Mantine's `Tabs` silently refuses to render a tab/panel whose `value` is an empty string --
    # regression coverage for exactly that: every tab must get a real, non-empty `value`.
    assert all(tab.value for tab in tabs)


# ---- panel_name_taken / unique_panel_name ----
#
# Two panels sharing a name break every id keyed by panel name alone (`PANEL_CONTENT`,
# `PANEL_ACCORDION`, ...) -- most visibly as a pattern-matching `MATCH` collision on whatever
# chart both panels happen to render identically. `create_panel`/`rename_panel` both guard against
# it via `panel_name_taken`; `rename_panel` also ignores the panel being renamed against itself.
# `unique_panel_name` is its counterpart for the "auto-create a panel" paths (a new tab created
# without picking existing panels, or a chart dropped onto a tab with no panel yet).


@pytest.mark.parametrize(
    ("name", "taken"),
    [pytest.param("a", True, id="existing"), pytest.param("c", False, id="new")],
)
def test_panel_name_taken_only_for_a_name_already_in_use(name: str, *, taken: bool) -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert page_model.panel_name_taken(panels, name) is taken


def test_panel_name_taken_ignores_the_named_panel_itself() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "b")]
    assert page_model.panel_name_taken(panels, "a", ignore="a") is False


def test_unique_panel_name_returns_base_when_free() -> None:
    panels = [PanelInstance[Any, Any](name="a")]
    assert page_model.unique_panel_name(panels, "b") == "b"


def test_unique_panel_name_suffixes_when_taken() -> None:
    panels = [PanelInstance[Any, Any](name=n) for n in ("a", "a (2)")]
    assert page_model.unique_panel_name(panels, "a") == "a (3)"


# ---- move_chart_to_tab ----


def test_move_chart_to_tab_appends_to_an_existing_panel_in_that_tab() -> None:
    panels = [
        _panel_with_charts("x", "y").model_copy(update={"name": "src"}),
        PanelInstance[Any, Any](name="dst", tab="Images"),
    ]

    result = page_model.move_chart_to_tab(panels, "src", 0, "Images")

    by_name = {p.name: [c.parameters["column"] for c in p.charts] for p in result}
    assert by_name == {"src": ["y"], "dst": ["x"]}


def test_move_chart_to_tab_creates_a_panel_when_the_tab_is_empty() -> None:
    panels = [_panel_with_charts("x", "y").model_copy(update={"name": "src"})]

    result = page_model.move_chart_to_tab(panels, "src", 0, "Images")

    new_panel = next(p for p in result if p.tab == "Images")
    assert [c.parameters["column"] for c in new_panel.charts] == ["x"]
    assert [c.parameters["column"] for c in next(p for p in result if p.name == "src").charts] == ["y"]


def test_move_chart_to_tab_is_a_noop_for_an_out_of_range_index() -> None:
    panels = [_panel_with_charts("x").model_copy(update={"name": "src"})]
    assert page_model.move_chart_to_tab(panels, "src", 5, "Images") == panels


# ---- move_chart_to_panel ----


def test_move_chart_to_panel_inserts_at_a_specific_position() -> None:
    panels = [
        _panel_with_charts("x", "y").model_copy(update={"name": "src"}),
        _panel_with_charts("a", "b").model_copy(update={"name": "dst"}),
    ]

    result = page_model.move_chart_to_panel(panels, "src", 0, "dst", 0, after=False)

    by_name = {p.name: [c.parameters["column"] for c in p.charts] for p in result}
    assert by_name == {"src": ["y"], "dst": ["x", "a", "b"]}


def test_move_chart_to_panel_inserts_after_the_target_index() -> None:
    panels = [
        _panel_with_charts("x").model_copy(update={"name": "src"}),
        _panel_with_charts("a", "b").model_copy(update={"name": "dst"}),
    ]

    result = page_model.move_chart_to_panel(panels, "src", 0, "dst", 0, after=True)

    dst = next(p for p in result if p.name == "dst")
    assert [c.parameters["column"] for c in dst.charts] == ["a", "x", "b"]


def test_move_chart_to_panel_appends_when_target_index_is_none() -> None:
    panels = [
        _panel_with_charts("x").model_copy(update={"name": "src"}),
        _panel_with_charts("a").model_copy(update={"name": "dst"}),
    ]

    result = page_model.move_chart_to_panel(panels, "src", 0, "dst", None, after=True)

    dst = next(p for p in result if p.name == "dst")
    assert [c.parameters["column"] for c in dst.charts] == ["a", "x"]


def test_move_chart_to_panel_appends_into_an_empty_panel() -> None:
    panels = [
        _panel_with_charts("x").model_copy(update={"name": "src"}),
        PanelInstance[Any, Any](name="dst"),
    ]

    result = page_model.move_chart_to_panel(panels, "src", 0, "dst", None, after=True)

    dst = next(p for p in result if p.name == "dst")
    assert [c.parameters["column"] for c in dst.charts] == ["x"]
    assert next(p for p in result if p.name == "src").charts == []


def test_move_chart_to_panel_is_a_noop_for_an_out_of_range_index() -> None:
    panels = [
        _panel_with_charts("x").model_copy(update={"name": "src"}),
        PanelInstance[Any, Any](name="dst"),
    ]
    assert page_model.move_chart_to_panel(panels, "src", 5, "dst", None, after=True) == panels


def test_move_chart_to_panel_is_a_noop_for_an_unknown_target_panel() -> None:
    panels = [_panel_with_charts("x").model_copy(update={"name": "src"})]
    assert page_model.move_chart_to_panel(panels, "src", 0, "missing", None, after=True) == panels


def _badge_labels(node: Any) -> list[str]:  # noqa: ANN401
    """The text of every `Badge` in a serialized Dash component tree."""
    match node:
        case {"type": "Badge", "props": {"children": str(label)}}:
            return [label]
        case {"props": {"children": children}}:
            return _badge_labels(children)  # pyrefly: ignore [unknown-argument-type]
        case list():
            return [label for child in cast("list[Any]", node) for label in _badge_labels(child)]
        case _:
            return []


@pytest.mark.parametrize(("n_charts", "badges"), [(0, []), (1, ["1 chart"]), (126, ["126 charts"])])
def test_panel_header_says_how_many_charts_are_inside(n_charts: int, badges: list[str]) -> None:
    charts = [
        ChartInstance[Any, Any](chart_type=LineChart.name, parameters={"column": f"m{i}"})
        for i in range(n_charts)
    ]

    rendered = panel_view.panel_header(PanelInstance[Any, Any](name="train", charts=charts))

    assert _badge_labels(json.loads(cast("str", to_json(rendered)))) == badges


def test_apply_panel_sync_scopes_a_line_chart_under_its_title() -> None:
    plot = dmc.LineChart(data=[], dataKey="step", series=[], lineChartProps={"syncId": "step"})

    chart_view._apply_panel_sync(titled("loss", plot), panel_name="train", sync=True)

    assert plot.lineChartProps["syncId"] == "train:step"  # pyrefly: ignore [missing-attribute]
