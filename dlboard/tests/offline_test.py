"""End-to-end: a run logged to a folder with no server, then brought to one by syncing the folder (#703)."""

from __future__ import annotations

import atexit
import time
from typing import TYPE_CHECKING, override

import numpy as np
import pendulum
import pytest
import requests

from dlboard import models
from dlboard.client import _offline, run
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.client.artifacts.image import Image

if TYPE_CHECKING:
    from pathlib import Path

    from dlboard.conftest import BackendServer

pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")


def _stop(started: run.Run) -> None:
    """Stop a finished test run's shippers and exit hook, so nothing is left for interpreter exit."""
    atexit.unregister(started._exit_hook)
    shipping = started._shipping
    assert shipping is not None
    for shipper in (shipping.metrics, shipping.artifacts):
        shipper.worker.stop()


def _server_runs(backend_server: BackendServer, experiment: str) -> list[models.Run]:
    store = backend_server.store
    found = [e for p in store.get_projects() for e in store.get_experiments(p.id) if e.name == experiment]
    return [r for e in found for r in store.get_runs(e.id, run_ids=None)]


def test_a_run_logged_to_a_folder_reaches_a_server_whole_when_the_folder_is_synced(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    folder = tmp_path / "offline"
    with run.init(
        "air-gapped",
        "sweep",
        name="baseline",
        hparams={"lr": 0.1},
        tags=["baseline"],
        server_url=folder.as_uri(),
    ) as started:
        for step in range(3):
            started.log_metrics({"loss": 1.0 / (step + 1)}, step=step)
        started.log_artifact([Image(key="sample", image=np.zeros((4, 4, 3), dtype=np.uint8), step=0)])
    _stop(started)
    api = BasicDlboardAPI(backend_server.url)

    first = _offline.sync(api, folder)
    again = _offline.sync(api, folder)

    (synced,) = _server_runs(backend_server, "sweep")
    store = backend_server.store
    assert (synced.name, synced.status) == ("baseline", models.RunStatus.FINISHED)
    assert list(store.fetch_metrics(synced.experiment_id, run_ids=frozenset({synced.id})).step) == [0, 1, 2]
    (hparams,) = store.fetch_hyperparams(synced.experiment_id, run_ids=frozenset({synced.id}))
    assert hparams.hparams_dict == {"lr": 0.1}
    assert store.run_tags(frozenset({synced.id})) == {synced.id: ["baseline"]}
    assert (first.runs, again.calls) == (1, 0)
    deadline = time.monotonic() + 10  # the server records an upload a moment after accepting it
    while not list(store.fetch_artifacts(synced.experiment_id, run_ids=frozenset({synced.id}))):
        assert time.monotonic() < deadline, "the image never landed"
        time.sleep(0.1)


def test_a_sync_sends_a_line_cut_short_only_once_it_is_whole(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    offline = _offline.OfflineAPI(tmp_path / "offline")
    offline.whoami()
    project = offline.get_or_create_project("cut")
    experiment = offline.get_or_create_experiment(project.id, "cut")
    local = offline.create_run(models.NewRun(experiment_id=experiment.id, name="killed"))
    for step in range(2):
        offline.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment.id,
                    run_id=local.id,
                    step=step,
                    metrics={"loss": 1.0},
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
    (segment,) = (tmp_path / "offline").glob("run-*/*.jsonl")
    whole = segment.read_bytes()
    segment.write_bytes(whole[:-5])  # the process was killed writing its second line
    api = BasicDlboardAPI(backend_server.url)

    before = _offline.sync(api, tmp_path / "offline")
    segment.write_bytes(whole)
    after = _offline.sync(api, tmp_path / "offline")

    (synced,) = _server_runs(backend_server, "cut")
    assert (before.calls, after.calls) == (1, 1)
    assert list(backend_server.store.fetch_metrics(synced.experiment_id, run_ids=None).step) == [0, 1]


def _offline_run(folder: Path, name: str, steps: int) -> Path:
    """A run of `steps` metric calls logged to `folder`, as `OfflineAPI` writes one; its segment file."""
    offline = _offline.OfflineAPI(folder)
    offline.whoami()
    experiment = offline.get_or_create_experiment(offline.get_or_create_project(name).id, name)
    local = offline.create_run(models.NewRun(experiment_id=experiment.id, name=name))
    for step in range(steps):
        offline.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=experiment.id,
                    run_id=local.id,
                    step=step,
                    metrics={"loss": 1.0},
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
    (segment,) = folder.glob(f"run-{local.id}/*.jsonl")
    return segment


class _DownAfter(BasicDlboardAPI):
    """A server that goes down after taking `calls` metric batches: a sync stopped partway, as a killed one is."""

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


@pytest.mark.parametrize("taken", [0, 2], ids=["before-its-first-call", "partway"])
def test_a_sync_stopped_partway_makes_no_run_twice_and_sends_nothing_twice(
    backend_server: BackendServer, tmp_path: Path, taken: int
) -> None:
    _offline_run(tmp_path / "offline", f"stopped-{taken}", steps=4)

    with pytest.raises(requests.ConnectionError):
        _offline.sync(_DownAfter(backend_server.url, taken), tmp_path / "offline")
    resumed = _offline.sync(BasicDlboardAPI(backend_server.url), tmp_path / "offline")

    (synced,) = _server_runs(backend_server, f"stopped-{taken}")
    assert resumed.calls == 4 - taken
    assert list(backend_server.store.fetch_metrics(synced.experiment_id, run_ids=None).step) == [0, 1, 2, 3]


def test_a_call_that_can_never_be_sent_is_skipped_and_the_rest_synced(
    backend_server: BackendServer, tmp_path: Path
) -> None:
    segment = _offline_run(tmp_path / "offline", "skipped", steps=3)
    first, *rest = segment.read_bytes().splitlines(keepends=True)
    segment.write_bytes(b"".join([first, b'{"call": "metrics", "body": "not a batch"}\n', *rest]))

    with pytest.warns(UserWarning, match="skipped a call of run-"):
        _offline.sync(BasicDlboardAPI(backend_server.url), tmp_path / "offline")

    (synced,) = _server_runs(backend_server, "skipped")
    assert list(backend_server.store.fetch_metrics(synced.experiment_id, run_ids=None).step) == [0, 1, 2]


def test_a_run_is_offline_when_its_url_is_a_folder_or_dlboard_offline_dir_names_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(_offline.connect(tmp_path.as_uri()), _offline.OfflineAPI)
    assert isinstance(_offline.connect("http://localhost:8050"), BasicDlboardAPI)
    monkeypatch.setenv("DLBOARD_OFFLINE_DIR", str(tmp_path))
    connected = _offline.connect("http://localhost:8050")
    assert isinstance(connected, _offline.OfflineAPI)
    assert connected.folder == tmp_path


def test_an_unwritable_folder_fails_a_run_as_it_starts(tmp_path: Path) -> None:
    blocked = tmp_path / "file-not-folder"
    blocked.write_text("")

    with pytest.raises(OSError):  # noqa: PT011 -- which one the OS raises varies; that it raises at start does not
        _offline.OfflineAPI(blocked).whoami()
