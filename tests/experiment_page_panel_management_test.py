# pyright: reportPrivateUsage=false
"""Tests for the "Manage panels" list and the panel-mutation helpers it drives.

Deliberately a separate list outside the accordion, not layered onto AccordionControl (which
is a native full-width Mantine button and clips/fights any sibling or overlay content placed
in its row) — see `_panel_management_row`'s docstring.
"""

from __future__ import annotations

from typing import Any

from dltrack.models._view import ChartInstance, PanelInstance
from dltrack.plugins.pages.simple_experiment_page import (
    _add_chart_to_panel_by_name,
    _delete_panel_button_id,
    _move_panel,
    _move_panel_button_id,
    _panel_management_list,
    _rename_panel_button_id,
)
from tests.conftest import find_props as _find_props

# ---- "Manage panels" list: rename/delete/reorder controls work without opening a panel ----


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
