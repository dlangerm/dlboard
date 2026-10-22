"""The experiment page's pure panel edits: values in, values out."""

from __future__ import annotations

import pandas as pd
import pytest

from dlboard.models._view import ChartInstance, PanelInstance
from dlboard.serve._pages._experiment import _page_model as page_model


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

    result = page_model.upsert_chart(panel, "p", index, new_chart)

    assert result.charts[index].parameters == {"column": "new"}
    untouched = [i for i in range(3) if i != index]
    assert [result.charts[i].parameters["column"] for i in untouched] == [f"m{i}" for i in untouched]


def test_upsert_chart_appends_when_index_is_none() -> None:
    panel = _panel("p", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    result = page_model.upsert_chart(panel, "p", None, new_chart)

    assert len(result.charts) == 2
    assert result.charts[-1].parameters == {"column": "new"}


def test_upsert_chart_ignores_other_panels() -> None:
    panel = _panel("other", 1)
    new_chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "new"})

    assert page_model.upsert_chart(panel, "p", 0, new_chart) is panel


def test_a_settings_change_writes_only_what_it_sets_and_keeps_every_other_key() -> None:
    stored = {"hparam-table-selected": ["lr"], "open_panel": ["a"], "from-a-newer-version": True}

    merged = page_model.DocumentSettings(excluded_runs=[3]).merged_into(stored)

    assert merged == {**stored, "excluded_runs": [3]}


def test_selecting_the_newest_runs_again_is_written_as_no_selection_not_left_out() -> None:
    stored = {"selected_runs": [3, 4], "excluded_runs": [9]}

    assert page_model.DocumentSettings(selected_runs=None).merged_into(stored) == {
        "selected_runs": None,
        "excluded_runs": [9],
    }
    assert page_model.DocumentSettings(hparam_columns=["lr"]).merged_into(stored) == {
        **stored,
        "hparam-table-selected": ["lr"],
    }


def test_a_view_saved_before_selections_loads_with_none_and_keeps_its_exclusions() -> None:
    document = page_model.DocumentSettings.model_validate({"excluded_runs": [3]})

    assert (document.selected_runs, document.excluded_runs) == (None, [3])


def test_a_chart_is_found_by_its_id_wherever_it_now_sits_and_none_if_it_is_gone() -> None:
    panels = [_panel("a", 2), _panel("b", 3)]
    chart = panels[1].charts[2]

    location = page_model.locate_chart(panels, chart.id)

    assert location is not None
    assert (location.panel.name, location.index, location.chart) == ("b", 2, chart)
    assert page_model.locate_chart(panels, "gone") is None


def test_charts_saved_with_one_config_each_keep_an_id_of_their_own_and_are_found_by_it() -> None:
    chart = ChartInstance[pd.DataFrame, object](chart_type="line", parameters={"column": "loss"})
    page = page_model.BasicExperimentPage(
        id=0,
        experiment_id=0,
        owner_id=None,
        name="",
        panels=[PanelInstance(name="a", charts=[chart]), PanelInstance(name="b", charts=[chart])],
    )
    ids = [panel.charts[0].id for panel in page.panels]

    assert len(set(ids)) == 2
    reloaded = page_model.BasicExperimentPage.model_validate_json(page.model_dump_json())
    assert [panel.charts[0].id for panel in reloaded.panels] == ids  # the same on every load
    location = page_model.locate_chart(page.panels, ids[1])
    assert location is not None
    assert location.panel.name == "b"
