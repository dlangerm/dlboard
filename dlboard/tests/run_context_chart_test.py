"""The run context chart: what produced each run shown, read for those runs alone, a fact a row and a run a column."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from dlboard import models
from dlboard.conftest import props
from dlboard.plugins.charts.run_context import RunContextChart
from dlboard.serve._pages._experiment import _panel_data as panel_data

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

RunContextChart.register(allow_override=True)


def _run(store: SQLLiteStore, experiment_id: int, commit: str | None, *, diff: bool = False) -> int:
    """A run named for its commit, which recorded a context (with `commit`, unless `None`) and, if `diff`, its changes."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id, name=commit or "none"))
    if commit is not None:
        store.record_run_context(
            models.NewRunContext(
                run_id=run.id,
                experiment_id=experiment_id,
                git_commit=commit,
                git_dirty=diff,
                command=["train.py", "--lr", "0.1"],
                packages={"numpy": "2.0.0", "torch": "2.4.0"},
            )
        )
    if diff:
        store.log_artifact_refs(
            [
                models.Artifact(
                    key=models.RUN_CONTEXT_DIFF_KEY,
                    fname="git.diff",
                    run_id=run.id,
                    experiment_id=experiment_id,
                    step=0,
                    ref="r://diff",
                    tags={models.FILE_KIND_TAG: models.FileKind.DIFF.value},
                )
            ]
        )
    return run.id


def _render(store: SQLLiteStore, experiment_id: int, run_ids: set[int], **settings: Any) -> list[Any]:  # noqa: ANN401
    """The chart's parts, drawn from what its panel reads of `run_ids`: the table (or what it says instead), then links."""
    chart = models.ChartInstance[pd.DataFrame, object](chart_type=RunContextChart.name, parameters=settings)
    panel = models.PanelInstance[pd.DataFrame, object](name="p", charts=[chart])
    frame = panel_data.fetch_panel_data(store, experiment_id, panel, run_ids=frozenset(run_ids)).for_chart(
        chart
    )
    return list(props(chart.render(frame))["children"])


def _facts(table: Any) -> dict[tuple[str, str], list[str | None]]:  # noqa: ANN401
    """Each row of the table, by its section and fact, with each run's value, in column order."""
    grid = props(table)
    runs = [column["field"] for column in grid["columnDefs"][2:]]
    return {(row["section"], row["fact"]): [row[run] for run in runs] for row in grid["rowData"]}


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_each_run_shown_with_a_context_is_a_column_of_its_facts_with_its_changes_linked(
    store: SQLLiteStore, experiment_id: int
) -> None:
    clean, dirty = _run(store, experiment_id, "abc"), _run(store, experiment_id, "def", diff=True)
    unrecorded, not_shown = _run(store, experiment_id, None), _run(store, experiment_id, "zzz")

    table, links = _render(store, experiment_id, {clean, dirty, unrecorded})

    assert [column["headerName"] for column in props(table)["columnDefs"]] == [
        "Section",
        "Fact",
        "abc",
        "def",
    ]
    facts = _facts(table)
    assert facts["Git", "commit"] == ["abc", "def"]
    assert facts["Git", "dirty"] == ["no", "yes"]
    assert facts["Command", "command"] == ["train.py --lr 0.1"] * 2
    assert facts["Packages", "torch"] == ["2.4.0"] * 2
    assert not any("zzz" in values for values in facts.values()), not_shown
    (link,) = [part for part in props(links)["children"] if "href" in props(part)]
    assert props(link)["href"].startswith("/artifact/")


@pytest.mark.parametrize(
    ("settings", "listed"),
    [
        ({"only_differences": True}, [("Git", "commit")]),
        ({"git": False, "command": False}, [("Packages", "numpy"), ("Packages", "torch")]),
    ],
)
def test_its_settings_say_which_facts_it_lists(
    store: SQLLiteStore, experiment_id: int, settings: dict[str, Any], listed: list[tuple[str, str]]
) -> None:
    runs = {_run(store, experiment_id, "abc"), _run(store, experiment_id, "def")}

    (table,) = _render(store, experiment_id, runs, **settings)

    assert list(_facts(table)) == listed


def test_runs_shown_that_recorded_no_context_are_said_to_have_none(
    store: SQLLiteStore, experiment_id: int
) -> None:
    (message,) = _render(store, experiment_id, {_run(store, experiment_id, None)})

    assert props(message)["children"] == "No context was recorded for the runs shown"


def test_an_experiment_offers_the_chart_once_a_live_run_recorded_a_context(
    store: SQLLiteStore, experiment_id: int, admin: models.User
) -> None:
    _run(store, experiment_id, None)
    assert not store.has_run_contexts(experiment_id)

    recorded = _run(store, experiment_id, "abc")
    assert store.has_run_contexts(experiment_id)

    store.delete_run(recorded, admin)
    assert not store.has_run_contexts(experiment_id)
