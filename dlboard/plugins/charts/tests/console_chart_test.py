"""The console chart: each run's newest chunk on show, and the chunk beside it read alone, a bounded span of steps away."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from dash._utils import to_json

from dlboard import models
from dlboard.models import (
    CONSOLE_KEY,
    EXPERIMENT_COLUMN,
    RUN_NAME_COLUMN,
    WindowAt,
    artifact_column,
    artifact_logged_column,
)
from dlboard.plugins.charts.console import (
    SEEK_STEPS,
    ConsoleChart,
    ConsoleChartSettings,
    Shown,
    _PageWay,
    latest_chunks,
    neighbour,
)
from dlboard.serve._backend import _artifact_download

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_SETTINGS = ConsoleChartSettings()


def _frame() -> pd.DataFrame:
    """Two runs' chunks as a panel lists them: every step each logged, and the newest one's URL alone."""
    return pd.DataFrame(
        {
            "run_id": [7, 7, 7, 3],
            "step": [0, 1, 2, 0],
            EXPERIMENT_COLUMN: [1, 1, 1, 1],
            RUN_NAME_COLUMN: ["late", "late", "late", "early"],
            artifact_logged_column(CONSOLE_KEY): [True, True, True, True],
            artifact_column(CONSOLE_KEY): [float("nan"), float("nan"), "/artifact/9?v=c", "/artifact/4?v=a"],
        }
    )


def test_each_run_shows_its_newest_chunk_in_the_order_the_panel_lists_them() -> None:
    latest = latest_chunks(_SETTINGS, _frame())

    assert [(shown.run_id, shown.step, shown.url) for shown in latest.values()] == [
        (7, 2, "/artifact/9?v=c"),
        (3, 0, "/artifact/4?v=a"),
    ]


def test_the_run_picker_names_each_run_and_starts_on_the_first() -> None:
    rendered: dict[str, Any] = json.loads(cast("str", to_json(ConsoleChart.render(_SETTINGS, _frame()))))
    select: dict[str, Any] = rendered["props"]["children"][0]["props"]["children"][0]["props"]

    assert [option["label"] for option in select["data"]] == ["late", "early"]
    assert select["value"] == "7"


def test_runs_that_printed_nothing_say_so() -> None:
    rendered = cast("str", to_json(ConsoleChart.render(_SETTINGS, pd.DataFrame())))

    assert "Nothing was printed" in rendered


def test_the_panel_reads_only_each_runs_newest_chunk() -> None:
    (window,) = ConsoleChart.data_request(_SETTINGS).artifact_windows

    assert (window.key, window.steps, window.at) == (CONSOLE_KEY, 1, WindowAt.LATEST)


def test_only_the_console_key_is_charted_as_a_console() -> None:
    def artifact(key: str) -> models.Artifact:
        return models.Artifact(
            id=1, key=key, fname="f.txt", run_id=1, experiment_id=1, step=0, ref="file:///x"
        )

    assert ConsoleChart.can_display_artifact(artifact(CONSOLE_KEY))
    assert not ConsoleChart.can_display_artifact(artifact("notes.txt"))


def _chunks(store: SQLLiteStore, experiment_id: int, steps: list[int]) -> models.Run:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        models.Artifact(
            key=CONSOLE_KEY,
            fname=f"console-{step:06d}.txt",
            run_id=run.id,
            experiment_id=experiment_id,
            step=step,
            ref=f"file:///console-{run.id}-{step}.txt",
        )
        for step in steps
    )
    return run


def test_older_and_newer_read_the_chunk_beside_and_skip_one_that_was_lost(
    monkeypatch: pytest.MonkeyPatch, store: SQLLiteStore, experiment_id: int
) -> None:
    # `relative_path` reads Dash's process-global config, which only exists once some test has built a Dash app.
    monkeypatch.setattr(_artifact_download, "relative_path", str)
    run = _chunks(store, experiment_id, [0, 1, 3, 4])
    on_show = Shown(experiment_id=experiment_id, run_id=run.id, key=CONSOLE_KEY, step=3, url="/artifact/x")

    older = neighbour(store, on_show, _PageWay.OLDER)
    newer = neighbour(store, on_show, _PageWay.NEWER)

    assert (older.step if older else None, newer.step if newer else None) == (1, 4)
    assert older is not None
    assert older.url.startswith("/artifact/")
    assert neighbour(store, on_show.model_copy(update={"step": 0}), _PageWay.OLDER) is None
    assert neighbour(store, on_show.model_copy(update={"step": 4 + SEEK_STEPS}), _PageWay.NEWER) is None
