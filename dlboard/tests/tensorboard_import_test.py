"""End-to-end: a TensorBoard logdir imported into a live dlboard backend, and imported again as its runs go on (#702)."""

from __future__ import annotations

import re
import shutil
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
import requests

from dlboard import models
from dlboard.client import _tfevents, tensorboard
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.client.tensorboard import import_logdir

if TYPE_CHECKING:
    from dlboard.conftest import BackendServer

_FIXTURE = Path(__file__).parents[1] / "client" / "tests" / "tensorboard_logdir"


def _logdir(tmp_path: Path) -> Path:
    logdir = tmp_path / "my-sweep"
    shutil.copytree(_FIXTURE, logdir)
    return logdir


def _runs(backend_server: BackendServer, experiment_id: int) -> dict[str, models.Run]:
    return {run.name or "": run for run in backend_server.store.get_runs(experiment_id, run_ids=None)}


def test_a_logdir_becomes_an_experiment_of_a_run_for_each_directory_of_event_files(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    api = BasicDlboardAPI(backend_server.url)

    report = import_logdir(api, _logdir(tmp_path), project="imported")

    store = backend_server.store
    (experiment,) = (e for e in store.search_experiments(1, limit=10).experiments if e.name == "my-sweep")
    runs = _runs(backend_server, experiment.id)
    assert experiment.source is models.ExperimentSource.TENSORBOARD
    assert sorted(runs) == ["baseline", "sweep/lr=0.01"]
    assert runs["baseline"].status is models.RunStatus.FINISHED
    assert (report.runs, report.scalars, report.histograms, report.images, report.hparams) == (2, 13, 2, 1, 1)
    assert dict(report.unread) == {"text": 1}
    metrics = store.fetch_metrics(
        experiment.id, keys=frozenset({"loss"}), run_ids=frozenset({runs["baseline"].id})
    )
    assert list(metrics.step) == [0, 1, 2, 3, 4]
    (hparams,) = store.fetch_hyperparams(experiment.id, run_ids=frozenset({runs["baseline"].id}))
    assert hparams.hparams_dict == {"lr": 0.1, "optimizer": "adam", "deep": True}
    assert store.summarize_histogram_keys(experiment.id) == ["grad"]
    deadline = time.monotonic() + 10  # the server records an upload a moment after accepting it
    while not list(store.fetch_artifacts(experiment.id, run_ids=None)):
        assert time.monotonic() < deadline, "the image never landed"
        time.sleep(0.1)


class _DownAfter(BasicDlboardAPI):
    """A server that goes down after taking `calls` metric batches: an import stopped partway, as a killed one is."""

    def __init__(self, url: str, calls: int) -> None:
        super().__init__(url)
        self.calls = calls

    @override
    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        if self.calls == 0:
            msg = "the server went down"
            raise requests.ConnectionError(msg)
        self.calls -= 1
        super().log_metric_batch(metrics)


@pytest.mark.parametrize("taken", [0, 1], ids=["before-its-first-batch", "partway-through-a-file"])
def test_an_import_stopped_partway_makes_no_run_twice_and_logs_no_step_twice(
    backend_server: BackendServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taken: int
) -> None:
    monkeypatch.setattr(tensorboard, "_BATCH", 2)  # several batches to each event file
    logdir, project = _logdir(tmp_path), f"stopped-{taken}"

    with pytest.raises(requests.ConnectionError):
        import_logdir(_DownAfter(backend_server.url, taken), logdir, project=project)
    import_logdir(BasicDlboardAPI(backend_server.url), logdir, project=project)

    store = backend_server.store
    (experiment,) = (
        e
        for p in store.get_projects()
        if p.name == project
        for e in store.search_experiments(p.id, limit=10).experiments
    )
    runs = _runs(backend_server, experiment.id)
    assert sorted(runs) == ["baseline", "sweep/lr=0.01"]
    loss = store.fetch_metrics(
        experiment.id, keys=frozenset({"loss"}), run_ids=frozenset({runs["baseline"].id})
    )
    assert list(loss.step) == [0, 1, 2, 3, 4]


def test_a_record_that_cannot_be_read_is_skipped_and_the_rest_imported(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    logdir = _logdir(tmp_path)
    (written,) = (logdir / "sweep" / "lr=0.01").iterdir()
    garbled = b"\x0a\xff"  # a field whose length runs past the record's end
    with written.open("ab") as file:
        file.write(struct.pack("<QI", len(garbled), 0) + garbled + bytes(4))

    with pytest.warns(UserWarning, match=f"skipped the record at byte \\d+ of {re.escape(str(written))}"):
        report = import_logdir(BasicDlboardAPI(backend_server.url), logdir, project="garbled")

    assert report.scalars == 13


def test_importing_again_brings_only_what_a_run_wrote_since(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    logdir = _logdir(tmp_path)
    (written,) = (logdir / "sweep" / "lr=0.01").iterdir()
    whole = written.read_bytes()
    ends = [end for end, _ in _tfevents.records(written, 0)]
    written.write_bytes(whole[: ends[-2] + 3])  # the run is mid-write: its last record is begun, not finished

    first = import_logdir(api, logdir, project="followed")
    written.write_bytes(whole)  # the run writes the rest
    second = import_logdir(api, logdir, project="followed")
    third = import_logdir(api, logdir, project="followed")

    (experiment,) = (
        e for e in backend_server.store.search_experiments(1, limit=10).experiments if e.name == "my-sweep"
    )
    run = _runs(backend_server, experiment.id)["sweep/lr=0.01"]
    metrics = backend_server.store.fetch_metrics(experiment.id, run_ids=frozenset({run.id}))
    # baseline's 10 and two of the sweep's 3, then its third once it is written, then nothing.
    assert (first.scalars, second.scalars, third.scalars) == (12, 1, 0)
    assert list(metrics.step) == [0, 1, 2]
