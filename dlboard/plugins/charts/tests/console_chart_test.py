"""The console chart: each run opens on its newest chunk or its first, and pages through the rest a chunk at a time."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
import pytest
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
    ConsoleOrder,
    Shown,
    _PageWay,
    opening_chunks,
    paged,
)

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_SETTINGS = ConsoleChartSettings()


def _frame(opens_on: int = 2) -> pd.DataFrame:
    """Two runs' chunks as a panel lists them: every step each logged, and the URL of the one each opens on alone."""
    nan = float("nan")
    return pd.DataFrame(
        {
            "run_id": [7, 7, 7, 3],
            "step": [0, 1, 2, 0],
            EXPERIMENT_COLUMN: [1, 1, 1, 1],
            RUN_NAME_COLUMN: ["late", "late", "late", "early"],
            artifact_logged_column(CONSOLE_KEY): [True, True, True, True],
            artifact_column(CONSOLE_KEY): [
                *(f"/a/{s}" if s == opens_on else nan for s in range(3)),
                "/a/early",
            ],
        }
    )


@pytest.mark.parametrize(
    ("order", "at", "opens_on"),
    [(ConsoleOrder.NEWEST_FIRST, WindowAt.LATEST, 2), (ConsoleOrder.OLDEST_FIRST, WindowAt.EARLIEST, 0)],
)
def test_each_run_opens_on_the_chunk_its_order_reads_first_knowing_where_its_others_are(
    order: ConsoleOrder, at: WindowAt, opens_on: int
) -> None:
    settings = ConsoleChartSettings(order=order)
    (window,) = ConsoleChart.data_request(settings).artifact_windows

    shown = opening_chunks(settings, _frame(opens_on))

    assert (window.key, window.steps, window.at) == (CONSOLE_KEY, 1, at)
    assert [(s.run_id, s.step, s.first_step, s.last_step) for s in shown.values()] == [
        (7, opens_on, 0, 2),
        (3, 0, 0, 0),
    ]


def test_the_run_picker_names_each_run_and_starts_on_the_first() -> None:
    rendered: dict[str, Any] = json.loads(cast("str", to_json(ConsoleChart.render(_SETTINGS, _frame()))))
    select: dict[str, Any] = rendered["props"]["children"][0]["props"]["children"][0]["props"]

    assert [option["label"] for option in select["data"]] == ["late", "early"]
    assert select["value"] == "7"


def test_runs_that_printed_nothing_say_so() -> None:
    rendered = cast("str", to_json(ConsoleChart.render(_SETTINGS, pd.DataFrame())))

    assert "Nothing was printed" in rendered


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


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_the_pager_goes_a_chunk_at_a_time_skipping_one_lost_or_to_either_end(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = _chunks(store, experiment_id, [0, 1, 3, 4, 6])
    # As the panel read it, before chunk 6 was logged.
    on_show = Shown(
        experiment_id=experiment_id,
        run_id=run.id,
        key=CONSOLE_KEY,
        step=3,
        url="/a",
        first_step=0,
        last_step=4,
    )

    went = {way: paged(store, on_show, way) for way in _PageWay}

    assert {way: (s.step, s.first_step, s.last_step) for way, s in went.items() if s} == {
        _PageWay.OLDEST: (0, 0, 6),
        _PageWay.OLDER: (1, 0, 4),
        _PageWay.NEWER: (4, 0, 4),
        _PageWay.NEWEST: (6, 0, 6),
    }
    assert all(s is not None and s.url.startswith("/artifact/") for s in went.values())
    assert paged(store, on_show.model_copy(update={"step": 0}), _PageWay.OLDER) is None
    assert paged(store, on_show.model_copy(update={"step": 6 + SEEK_STEPS}), _PageWay.NEWER) is None
