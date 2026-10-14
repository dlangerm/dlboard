"""The experiment page's pattern-matching id families: what a page renders is what its callbacks listen for."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dash import ALL, MATCH

from dlboard.models import ColumnKind
from dlboard.serve._component_ids import PatternId
from dlboard.serve._pages._experiment import _chart_editor_modal as editor
from dlboard.serve._pages._experiment import _chart_suggestions as suggestions
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _experiment_page_state as state
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _panel_view as panel_view

if TYPE_CHECKING:
    from types import ModuleType

_MODULES: tuple[ModuleType, ...] = (state, editor, suggestions)


def _patterns() -> list[PatternId[ids.ExperimentPage]]:
    return [value for module in _MODULES for value in vars(module).values() if isinstance(value, PatternId)]


@pytest.mark.parametrize(
    ("rendered", "pattern"),
    [
        (panel_view.PANEL_CONTENT.of("Losses"), panel_view.PANEL_CONTENT),
        (chart_view.CHART_BODY.of("Losses", 2), chart_view.CHART_BODY),
        (panel_view.PANEL_ACCORDION.of(""), panel_view.PANEL_ACCORDION),
        (editor.CHART_PARAM.of("column"), editor.CHART_PARAM),
        (suggestions._ADD_SUGGESTION.of(ColumnKind.METRIC, "loss"), suggestions._ADD_SUGGESTION),
    ],
)
def test_a_rendered_id_has_exactly_the_keys_its_wildcards_match_on(
    rendered: dict[str, object], pattern: PatternId[ids.ExperimentPage]
) -> None:
    assert rendered.keys() == pattern.all().keys() == pattern.match().keys()
    assert rendered["type"] == pattern.type
    assert {pattern.all()[key] for key in pattern.KEYS} == {ALL}
    assert {pattern.match()[key] for key in pattern.KEYS} == {MATCH}


def test_no_two_id_families_share_a_type() -> None:
    types = [pattern.type for pattern in _patterns()]

    assert len(types) == len(set(types))
