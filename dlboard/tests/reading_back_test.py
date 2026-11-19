"""End-to-end: what was logged, read back through the REST API a page at a time, by the client (#701)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI, pages
from dlboard.plugins.backend import _reads

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.conftest import BackendServer


def _experiment(api: BasicDlboardAPI, name: str) -> int:
    project = api.get_or_create_project(f"reading {name}")
    return api.get_or_create_experiment(project.id, name).id


def _log(api: BasicDlboardAPI, run: models.Run, metrics: dict[str, float], step: int) -> None:
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=run.experiment_id,
                run_id=run.id,
                step=step,
                metrics=metrics,
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )


def test_a_series_is_read_back_whole_in_step_order_a_page_at_a_time(backend_server: BackendServer) -> None:
    api = BasicDlboardAPI(backend_server.url)
    run = api.create_run(models.NewRun(experiment_id=_experiment(api, "series")))
    for step in [
        *range(23),
        22,
    ]:  # the last step is logged twice: both values are read back, in the order logged
        _log(api, run, {"loss": float(step)}, step)

    read = list(pages(lambda cursor: api.series(run.id, "loss", cursor=cursor, limit=10)))

    assert [len(page.items) for page in read] == [10, 10, 4]
    assert [point.step for page in read for point in page.items] == [*range(23), 22]


def test_a_page_never_holds_more_than_the_server_allows(
    backend_server: BackendServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    run = api.create_run(models.NewRun(experiment_id=_experiment(api, "capped")))
    for step in range(5):
        _log(api, run, {"loss": 1.0}, step)
    monkeypatch.setattr(_reads, "MAX_VALUES", 2)

    page = api.series(run.id, "loss", limit=1000)

    assert len(page.items) == 2
    assert page.next_cursor is not None


def test_runs_are_listed_newest_first_and_found_by_tag(backend_server: BackendServer) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment_id = _experiment(api, "runs")
    start = pendulum.now("UTC").subtract(hours=1)
    runs = [
        api.create_run(models.NewRun(experiment_id=experiment_id, created_at=start.add(minutes=i)))
        for i in range(5)
    ]
    for tagged in runs[:2]:
        api.mark_run(tagged.id, tags=["baseline"])

    listed = [
        run.id for page in pages(lambda c: api.runs(experiment_id, cursor=c, limit=2)) for run in page.items
    ]
    found = [run.id for page in api.iter_runs(experiment_id, tags=["Baseline"]) for run in page.items]

    assert listed == [run.id for run in reversed(runs)]
    assert found == [runs[1].id, runs[0].id]


def test_projects_and_their_experiments_are_listed_a_page_at_a_time(backend_server: BackendServer) -> None:
    api = BasicDlboardAPI(backend_server.url)
    project = api.get_or_create_project("listed")
    made = [api.get_or_create_experiment(project.id, f"exp-{i}").id for i in range(3)]

    listed = [
        e.id for page in pages(lambda c: api.experiments(project.id, cursor=c, limit=2)) for e in page.items
    ]

    assert sorted(listed) == sorted(made)
    assert [found.name for found in api.projects(search="LISTED").items] == ["listed"]


def test_a_runs_summary_hyperparameters_and_keys_are_read_without_its_series(
    backend_server: BackendServer,
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment_id = _experiment(api, "summary")
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    api.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    for step in range(3):
        _log(api, run, {"acc": step / 10, "loss": 1.0 - step / 10}, step)

    keys = [
        key
        for page in pages(lambda c: api.metric_keys(experiment_id, cursor=c, limit=1))
        for key in page.items
    ]
    summary = {latest.key: latest.value for latest in api.summary(run.id).items}

    assert api.hyperparams(run.id) == {"lr": 0.1}
    assert keys == ["acc", "loss"]
    assert summary == pytest.approx({"acc": 0.2, "loss": 0.8})


def test_a_runs_artifacts_are_listed_by_step_then_those_of_no_step_and_their_bytes_read(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    experiment_id = _experiment(api, "artifacts")
    run = api.create_run(models.NewRun(experiment_id=experiment_id))
    steps = [2, None, 0, 1]
    for step in steps:
        path = tmp_path / f"file-{step}.txt"
        path.write_text(f"logged at {step}")
        artifact = models.NewArtifact(
            key="files", fname=path.name, run_id=run.id, experiment_id=experiment_id, step=step
        )
        api.log_artifact_batch([(artifact, path)])
    deadline = time.monotonic() + 10  # the server records an upload a moment after accepting it
    while len(list(backend_server.store.fetch_artifacts(experiment_id, run_ids=None))) < len(steps):
        assert time.monotonic() < deadline, "the uploads never landed"
        time.sleep(0.1)

    listed = [
        a for page in pages(lambda c: api.artifacts(run.id, "files", cursor=c, limit=3)) for a in page.items
    ]

    assert [artifact.step for artifact in listed] == [0, 1, 2, None]
    assert api.artifact_keys(experiment_id).items == ["files"]
    assert api.artifact_bytes(listed[-1].id) == b"logged at None"


def test_a_series_frame_says_when_it_stopped_short_of_the_whole_series(backend_server: BackendServer) -> None:
    api = BasicDlboardAPI(backend_server.url)
    run = api.create_run(models.NewRun(experiment_id=_experiment(api, "frame")))
    for step in range(4):
        _log(api, run, {"loss": float(step)}, step)

    whole = api.series_frame(run.id, "loss", max_rows=10)
    short = api.series_frame(run.id, "loss", max_rows=3)

    assert (list(whole.frame["step"]), whole.complete) == ([0, 1, 2, 3], True)
    assert (list(short.frame["step"]), short.complete) == ([0, 1, 2], False)
