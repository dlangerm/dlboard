"""The navbar's run-comparison table: its rows, the columns it offers, and when it redraws."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.models import HyperParams, NewHyperParams, Run
from dlboard.plugins.charts.file_list import FileListChart
from dlboard.plugins.charts.image_series import ImageChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.serve._pages._experiment import _column_catalog as column_catalog
from dlboard.serve._pages._experiment import _runs_table as run_table

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")

LineChart.register(allow_override=True)
FileListChart.register(allow_override=True)
ImageChart.register(allow_override=True)


@pytest.fixture(autouse=True)
def _plain_run_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """Links to a run are plain paths, with no running Dash app for `relative_path` to read the prefix of."""
    monkeypatch.setattr("dlboard.serve._pages._experiment._focus.relative_path", str)


def _run(run_id: int, name: str | None = None) -> Run:
    return Run(id=run_id, experiment_id=1, name=name, created_at=_TS)


def _fields(run_id: int, *, drawn: bool = False, focused: bool = False) -> dict[str, object]:
    """The fields every row of a running run has besides its id and name: how it is drawn, and where it links."""
    return {
        "_drawn": drawn,
        "_focused": focused,
        "_href": f"/experiment/1?run={run_id}",
        "_status": "running",
        "_duration_s": None,
        "_status_text": "running",
        "_status_detail": "running",
    }


def _listed(
    runs: list[Run],
    hparams: list[HyperParams] | None = None,
    *,
    shown: frozenset[int] = frozenset(),
    focus: int | None = None,
) -> run_table._Listed:
    selection = run_table._Selection(shown, chosen=False, focus=focus, newer_hidden=0)
    return run_table._Listed(runs, hparams or [], selection, None)


def test_build_hparam_rows_merges_hparams_and_latest_metrics() -> None:
    runs = [_run(1), _run(2)]
    hparams_by_run = {
        1: HyperParams(
            id=1,
            run_id=1,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(1, 1, {"lr": 0.1}).raw_hparams,
        ),
        2: HyperParams(
            id=2,
            run_id=2,
            experiment_id=1,
            raw_hparams=NewHyperParams.from_raw(2, 1, {"lr": 0.2}).raw_hparams,
        ),
    }
    latest_metrics = {1: {"loss": 0.5}}

    rows = run_table._build_hparam_rows(1, _listed(runs), hparams_by_run, latest_metrics)

    assert rows == [
        {"run_id": 1, "run_name": "Run 1", **_fields(1), "lr": 0.1, "loss": 0.5},
        {"run_id": 2, "run_name": "Run 2", **_fields(2), "lr": 0.2},
    ]


def test_build_hparam_rows_includes_runs_with_no_logged_hyperparameters() -> None:
    """A run that hasn't called `log_hyperparams` yet must still show up, not disappear."""
    runs = [_run(1, name="baseline"), _run(2)]

    rows = run_table._build_hparam_rows(1, _listed(runs), hparams_by_run={}, latest_metrics={})

    assert rows == [
        {"run_id": 1, "run_name": "baseline", **_fields(1)},
        {"run_id": 2, "run_name": "Run 2", **_fields(2)},
    ]


def _log_a_metric(store: SQLLiteStore, experiment_id: int, run_id: int, key: str, step: int = 0) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={key: 1.0}, step=step, experiment_id=experiment_id, run_id=run_id, timestamp_utc=_TS
            )
        ]
    )


def test_load_hparam_view_data_shows_each_selected_metrics_latest_value(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Used to read only the run's highest-step row, blanking any metric not logged at that exact step."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "val/acc", step=5)
    _log_a_metric(store, experiment_id, run.id, "train/loss", step=9)

    _hk, _mk, rows = run_table._load_hparam_view_data(
        store, experiment_id, _listed([run]), selected=["val/acc", "train/loss"]
    )

    assert rows == [
        {
            "run_id": run.id,
            "run_name": run.name,
            **(_fields(run.id) | {"_href": f"/experiment/{experiment_id}?run={run.id}"}),
            "val/acc": 1.0,
            "train/loss": 1.0,
        }
    ]


def test_load_hparam_view_data_reports_available_metric_keys_without_fetching_values(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """`metric_keys` (for the Columns picker) must come from the cheap key listing, not a full
    fetch -- rows stay metric-free until a column is actually selected."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_a_metric(store, experiment_id, run.id, "loss")

    hparam_keys, metric_keys, rows = run_table._load_hparam_view_data(
        store, experiment_id, _listed([run]), selected=[]
    )

    assert metric_keys == ["loss"]
    assert hparam_keys == []
    assert rows == [
        {
            "run_id": run.id,
            "run_name": run.name,
            **(_fields(run.id) | {"_href": f"/experiment/{experiment_id}?run={run.id}"}),
        }
    ]


@pytest.mark.parametrize(
    ("selected", "offered"),
    [
        pytest.param([], ["lr"], id="the-newest-runs-keys"),
        pytest.param(["momentum"], ["lr", "momentum"], id="and-a-picked-key-only-an-older-run-has"),
    ],
)
def test_the_columns_picker_offers_the_newest_runs_keys_and_keeps_an_older_one_it_shows(
    store: SQLLiteStore,
    experiment_id: int,
    monkeypatch: pytest.MonkeyPatch,
    selected: list[str],
    offered: list[str],
) -> None:
    """Runs are unbounded, so their keys are read from the newest few; a column already shown stays shown (#454)."""
    monkeypatch.setattr(column_catalog, "SUMMARY_RUNS", 1)
    older, newer = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    store.log_hyperparams(NewHyperParams.from_raw(older.id, experiment_id, {"momentum": 0.9}))
    store.log_hyperparams(NewHyperParams.from_raw(newer.id, experiment_id, {"lr": 0.1}))
    hparams = list(store.fetch_hyperparams(experiment_id, run_ids=None))

    hparam_keys, _metric_keys, rows = run_table._load_hparam_view_data(
        store, experiment_id, _listed([older, newer], hparams), selected=selected
    )

    assert hparam_keys == offered
    assert [row.get("momentum") for row in rows] == [0.9, None]


@pytest.mark.parametrize(
    ("status", "ran_for_s", "expected"),
    [
        pytest.param(
            models.RunStatus.FINISHED,
            203,
            ("finished", 203, "3m 23s", "finished after 3m 23s"),
            id="finished",
        ),
        pytest.param(
            models.RunStatus.FAILED, 3725, ("failed", 3725, "✕ 1h 2m", "failed after 1h 2m"), id="failed"
        ),
        pytest.param(
            models.RunStatus.FINISHED,
            2 * 86400 + 3 * 3600,
            ("finished", 183600, "2d 3h", "finished after 2d 3h"),
            id="days",
        ),
        pytest.param(
            models.RunStatus.UNKNOWN,
            300,
            ("unknown", 300, "? 5m 0s", "ended without saying how after 5m 0s"),
            id="the-script-exited-without-saying-how",
        ),
        pytest.param(models.RunStatus.RUNNING, None, ("running", None, "running", "running"), id="running"),
        pytest.param(None, None, (None, None, "", ""), id="a-run-stored-before-status-existed"),
    ],
)
def test_build_hparam_rows_shows_how_a_run_ended_and_how_long_it_took(
    status: models.RunStatus | None, ran_for_s: int | None, expected: tuple[str | None, int | None, str, str]
) -> None:
    ended_at = None if ran_for_s is None else _TS.add(seconds=ran_for_s)
    run = _run(1).model_copy(update={"status": status, "ended_at": ended_at})

    (row,) = run_table._build_hparam_rows(1, _listed([run]), hparams_by_run={}, latest_metrics={})

    assert (row["_status"], row["_duration_s"], row["_status_text"], row["_status_detail"]) == expected


def test_the_runs_table_redraws_when_a_run_finishes() -> None:
    """Its signature is what gates a redraw, so a status or end time has to be part of it."""
    running = _run(1)
    finished = running.model_copy(
        update={"status": models.RunStatus.FINISHED, "ended_at": _TS.add(minutes=1)}
    )

    def signature(run: Run) -> str:
        return run_table._render_signature(1, _listed([run]), [])

    assert signature(running) != signature(finished)
    assert signature(finished) == signature(finished.model_copy())


def test_newer_hidden_counts_the_runs_logged_after_the_newest_one_a_selection_draws() -> None:
    """A selection made earlier leaves out every run logged after the newest it drew, and no others."""
    runs = [
        _run(1).model_copy(update={"created_at": _TS.subtract(hours=3)}),
        _run(2).model_copy(update={"created_at": _TS.subtract(hours=2)}),
        _run(3).model_copy(update={"created_at": _TS.subtract(hours=1)}),
        _run(4).model_copy(update={"created_at": _TS}),
    ]

    assert run_table._newer_hidden(runs, frozenset({1, 2})) == 2
    assert run_table._newer_hidden(runs, frozenset()) == 0


def test_the_columns_picker_offers_a_page_of_what_its_search_finds_and_keeps_its_picks() -> None:
    """Every metric name went to the browser with the picker (#416): a page of them, the rest a search away."""
    hparams = ["batch_size", "lr"]
    metrics = [f"metric_{i:05d}" for i in range(run_table.KEY_PICKER_LIMIT * 3)]

    listed = run_table._column_options(hparams, metrics)
    searched = run_table._column_options(hparams, metrics, search="00250", keep=["metric_00001", "lr"])

    assert [len(group["items"]) for group in listed] == [2, run_table.KEY_PICKER_LIMIT - 2]
    assert [group["items"] for group in searched] == [["lr"], ["metric_00001", "metric_00250"]]


def test_a_row_is_drawn_when_the_charts_draw_it_and_focused_when_the_page_is_opened_on_it() -> None:
    runs = [_run(1), _run(2), _run(3)]

    rows = run_table._build_hparam_rows(
        1, _listed(runs, shown=frozenset({2}), focus=2), hparams_by_run={}, latest_metrics={}
    )

    assert [(row["_drawn"], row["_focused"]) for row in rows] == [
        (False, False),
        (True, True),
        (False, False),
    ]


def test_the_runs_table_redraws_when_the_page_is_opened_on_a_run_or_a_view() -> None:
    """A link to a run stays in the view the page is of, and the run it links to is part of what the table is drawn from."""
    runs = [_run(1)]
    plain = run_table._render_signature(1, _listed(runs), [])

    assert run_table._render_signature(1, _listed(runs, shown=frozenset({1}), focus=1), []) != plain
    assert run_table._render_signature(1, _listed(runs)._replace(view_id=7), []) != plain


def test_drawing_changes_exactly_the_rows_whose_eye_changed_and_clears_a_focus() -> None:
    update = run_table._drawn_rows(frozenset({1, 2}), frozenset({2, 3}), focus=1)

    assert {row.run_id: row.values for row in update.rows} == {
        1: {"_drawn": False, "_focused": False},
        3: {"_drawn": True},
    }


@pytest.mark.parametrize(
    ("selected_rows", "expected"),
    [
        pytest.param(None, set(), id="nothing-yet"),
        pytest.param({"ids": ["3", "5"]}, {3, 5}, id="the-ids-the-table-was-seeded-with"),
        pytest.param([{"run_id": 4, "run_name": "a"}, {"run_id": 9}], {4, 9}, id="the-rows-once-reported"),
    ],
)
def test_the_runs_a_grid_has_selected_are_read_from_either_shape_it_reports(
    selected_rows: list[dict[str, int]] | dict[str, list[str]] | None, expected: set[int]
) -> None:
    assert run_table._picked_ids(selected_rows) == frozenset(expected)
