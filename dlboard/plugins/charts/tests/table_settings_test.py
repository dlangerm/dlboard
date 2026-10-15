"""Tests for a table chart's settings: what it asks the panel to fetch, and the old settings it still loads."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from dlboard.conftest import props as _props
from dlboard.models import ChartInstance, ChartTypeRegistry
from dlboard.plugins.charts._key_patterns import KeyPatterns
from dlboard.plugins.charts._table_settings import Aggregate, TableChartSettings, TableMode, TableRows
from dlboard.plugins.charts.table_chart import TableChart

# ---- what the table asks the panel to fetch ----


@pytest.mark.parametrize(
    ("settings", "columns", "hparams"),
    [
        pytest.param(TableChartSettings(), None, None, id="everything-by-default"),
        pytest.param(
            TableChartSettings(show_metrics=["loss", "acc"], show_hparams=["lr"]),
            {"loss", "acc", "step"},
            {"lr"},
            id="names",
        ),
        pytest.param(
            TableChartSettings(show_metrics=["train/*"], show_hparams=["m.*"]), None, None, id="prefixes"
        ),
        # Metrics and hparams are fetched independently: a table showing none of one kind fetches none of it.
        pytest.param(
            TableChartSettings(show_metrics=[], show_hparams=["lr"]), set(), {"lr"}, id="only-hparams"
        ),
        pytest.param(
            TableChartSettings(show_metrics=["loss"], show_hparams=[]),
            {"loss", "step"},
            set(),
            id="no-hparams",
        ),
        pytest.param(
            TableChartSettings(rows=TableRows.FIELD, row_field="epoch", show_metrics=[], show_hparams=[]),
            {"epoch", "step"},
            set(),
            id="the-row-field-is-fetched",
        ),
    ],
)
def test_the_metrics_and_hparams_a_table_asks_the_panel_to_fetch(
    settings: TableChartSettings, columns: set[str] | None, hparams: set[str] | None
) -> None:
    assert TableChart.hint_required_columns(settings) == columns
    assert TableChart.hint_required_hparams(settings) == hparams


@pytest.mark.parametrize(
    ("show_artifacts", "keys", "prefixes"),
    [
        pytest.param([], set(), set(), id="none-by-default"),
        pytest.param(["a", "b"], {"a", "b"}, set(), id="keys"),
        pytest.param(["eval/*"], set(), {"eval/"}, id="prefix"),
        pytest.param(["a", "eval/*", "*"], {"a"}, {"eval/", ""}, id="both-and-all"),
    ],
)
def test_the_artifact_keys_a_table_asks_the_panel_to_fetch(
    show_artifacts: list[str], keys: set[str], prefixes: set[str]
) -> None:
    settings = TableChartSettings(show_artifacts=show_artifacts)

    assert TableChart.hint_required_artifact_keys(settings) == keys
    assert TableChart.hint_required_artifact_key_prefixes(settings) == prefixes


# ---- patterns ----


@pytest.mark.parametrize(
    ("patterns", "selected"),
    [
        pytest.param(["b", "a"], ["b", "a"], id="names-in-the-order-asked"),
        pytest.param(["nope", "a"], ["a"], id="a-name-that-does-not-exist-is-skipped"),
        pytest.param(["eval/*"], ["eval/a", "eval/b"], id="prefix-in-key-order"),
        pytest.param(["eval/b", "eval/*"], ["eval/b", "eval/a"], id="never-twice"),
        pytest.param(["*"], ["a", "b", "eval/a", "eval/b"], id="all"),
        pytest.param(["step", "*"], ["step", "a", "b", "eval/a", "eval/b"], id="reserved-by-name-only"),
    ],
)
def test_patterns_select_names_and_prefixes(patterns: list[str], selected: list[str]) -> None:
    keys = ["a", "b", "eval/a", "eval/b", "step"]
    assert KeyPatterns(tuple(patterns)).select(keys, reserved={"step"}) == selected


# ---- what a row needs ----


@pytest.mark.parametrize(
    ("settings", "missing"),
    [
        pytest.param({"rows": "field"}, "row_field", id="a-field"),
        pytest.param({"rows": "artifact"}, "show_artifacts", id="artifacts"),
    ],
)
def test_rows_must_say_what_they_are_made_of(settings: dict[str, str], missing: str) -> None:
    with pytest.raises(ValidationError, match=missing):
        TableChartSettings.model_validate(settings)


# ---- tables saved before rows and columns: the same table, loaded as the settings that make it ----


@pytest.mark.parametrize(
    ("saved", "loaded"),
    [
        pytest.param({}, TableChartSettings(), id="nothing-set"),
        pytest.param({"page_size": 3}, TableChartSettings(page_size=3), id="only-a-page-size"),
        pytest.param(
            {"metrics": ["loss"], "hparams": ["lr"], "artifacts": ["img"]},
            TableChartSettings(show_metrics=["loss"], show_hparams=["lr"], show_artifacts=["img"]),
            id="runs",
        ),
        # Empty used to mean "all", unless only the other was named, which meant "only that one".
        pytest.param(
            {"metrics": ["loss"]},
            TableChartSettings(show_metrics=["loss"], show_hparams=["*"]),
            id="metrics-only",
        ),
        pytest.param(
            {"hparams": ["lr"]}, TableChartSettings(show_metrics=[], show_hparams=["lr"]), id="hparams-only"
        ),
        pytest.param(
            {"artifacts": ["img"]},
            TableChartSettings(show_artifacts=["img"]),
            id="artifacts-beside-everything",
        ),
        pytest.param(
            {"mode": "runs", "metrics": ["loss"], "pivot_on": "step", "page_size": 5},
            TableChartSettings(show_metrics=["loss"], page_size=5),
            id="runs-ignores-the-pivot",
        ),
        pytest.param(
            {"mode": "pivot", "pivot_on": "step", "pivot_metric": "loss"},
            TableChartSettings(
                rows=TableRows.FIELD,
                row_field="step",
                spread_by_run=True,
                show_metrics=["loss"],
                show_hparams=[],
                aggregate=Aggregate.MEAN,
            ),
            id="pivot",
        ),
        pytest.param(
            {"pivot_on": "step", "pivot_metric": "loss"},
            TableChartSettings(
                rows=TableRows.FIELD,
                row_field="step",
                spread_by_run=True,
                show_metrics=["loss"],
                show_hparams=[],
                aggregate=Aggregate.MEAN,
            ),
            id="pivot-saved-before-modes-existed",
        ),
        pytest.param(
            {"mode": "artifacts", "artifact_key": "eval/a.png"},
            TableChartSettings(
                rows=TableRows.ARTIFACT, show_artifacts=["eval/a.png"], show_metrics=[], show_hparams=[]
            ),
            id="artifacts-by-key",
        ),
        pytest.param(
            {"mode": TableMode.ARTIFACTS, "artifact_key": "eval/", "artifact_key_is_prefix": True},
            TableChartSettings(
                rows=TableRows.ARTIFACT, show_artifacts=["eval/*"], show_metrics=[], show_hparams=[]
            ),
            id="artifacts-by-prefix",
        ),
    ],
)
def test_a_table_saved_with_the_old_settings_loads_as_the_one_they_made(
    saved: dict[str, Any], loaded: TableChartSettings
) -> None:
    assert TableChartSettings.model_validate(saved) == loaded


@pytest.mark.parametrize(
    ("saved", "match"),
    [
        pytest.param({"mode": "pivot"}, "pivot_metric", id="pivot-says-nothing"),
        pytest.param({"mode": "pivot", "pivot_on": "step"}, "pivot_metric", id="pivot-no-metric"),
        pytest.param({"pivot_on": "step"}, "pivot_metric", id="legacy-pivot-no-metric"),
        pytest.param({"mode": "artifacts"}, "artifact_key", id="artifacts-no-key"),
        pytest.param(
            {"metrics": ["loss"], "show_metrics": ["acc"]}, "show_metrics", id="old-and-new-at-once"
        ),
    ],
)
def test_old_settings_that_made_no_table_are_still_an_error(saved: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        TableChartSettings.model_validate(saved)


def test_an_old_table_is_edited_as_the_settings_it_loads_as() -> None:
    TableChart.register(allow_override=True)
    chart = ChartInstance[Any, Any](chart_type="table", parameters={"hparams": ["lr"], "page_size": 3})

    editing = ChartTypeRegistry.current_parameters(chart)

    assert (editing["show_hparams"], editing["show_metrics"], editing["page_size"]) == (["lr"], [], 3)
    assert "hparams" not in editing


def test_a_table_saved_with_old_settings_renders_as_it_did() -> None:
    df = pd.DataFrame({"run_id": [1, 1], "step": [0, 1], "loss": [0.5, 0.4], "acc": [0.1, 0.2]})
    settings = TableChartSettings.model_validate(
        {"mode": "pivot", "pivot_on": "step", "pivot_metric": "loss"}
    )
    props = _props(TableChart.render(settings, df))

    assert {row["step"]: row["1::loss"] for row in props["rowData"]} == {0: 0.5, 1: 0.4}
