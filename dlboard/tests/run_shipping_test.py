"""End-to-end: a run `dlboard.client.run.init` started (real shipping processes) logging to a live dlboard backend."""

from __future__ import annotations

import atexit
import platform
import time
from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.client import run
from dlboard.client._console import ConsoleSettings
from dlboard.client._rest_api import BasicDlboardAPI

if TYPE_CHECKING:
    from dlboard.conftest import BackendServer

# Spawning the shipping processes re-imports this test session's heavy modules in each, routinely past the
# slow-startup warning's threshold on a test box -- expected here, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")


def _stop(started: run.Run) -> None:
    """Stop a finished test run's shippers and exit hook, so nothing is left for interpreter exit."""
    atexit.unregister(started._exit_hook)
    shipping = started._shipping
    assert shipping is not None
    for shipper in (shipping.metrics, shipping.artifacts):
        shipper.worker.stop()


def test_a_run_from_init_lands_in_the_project_it_names_and_ends_as_its_block_did(
    backend_server: BackendServer,
) -> None:
    with run.init(
        "plain", "exp", name="baseline", hparams={"lr": 0.1}, server_url=backend_server.url
    ) as started:
        for step in range(3):
            started.log_metrics({"loss": 1.0 / (step + 1)}, step=step)
    _stop(started)

    store = backend_server.store
    run_id, experiment_id = started.run_id, started.experiment_id
    assert run_id is not None
    assert experiment_id is not None
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    assert (experiment.name, experiment.source) == ("exp", models.ExperimentSource.PYTHON)
    stored = store.get_run(run_id)
    assert stored is not None
    assert (stored.name, stored.status) == ("baseline", models.RunStatus.FINISHED)
    assert list(store.fetch_metrics(experiment_id, run_ids=None).step) == [0, 1, 2]
    (hparams,) = store.fetch_hyperparams(experiment_id, run_ids=None)
    assert hparams.hparams_dict == {"lr": 0.1}
    (context,) = store.fetch_run_contexts(experiment_id, run_ids=frozenset({run_id}))
    assert context.python_version == platform.python_version()
    assert "dlboard-client" in context.packages


def test_what_a_run_prints_reaches_the_server_as_its_console(backend_server: BackendServer) -> None:
    settings = run.RunSettings(console=ConsoleSettings(enabled=True))
    with run.init("plain", "printed", server_url=backend_server.url, settings=settings) as started:
        print("loss went up")
    _stop(started)

    assert started.experiment_id is not None
    deadline = time.monotonic() + 10  # the server records an upload a moment after accepting it
    while not (chunks := list(backend_server.store.fetch_artifacts(started.experiment_id, run_ids=None))):
        assert time.monotonic() < deadline, "the console chunk never showed up"
        time.sleep(0.1)
    assert [(chunk.key, chunk.fname, chunk.step) for chunk in chunks] == [
        (models.CONSOLE_KEY, "console-000000.txt", 0)
    ]


def test_a_heartbeat_keeps_a_run_heard_from_and_a_gone_run_answers_that_nobody_is_listening(
    backend_server: BackendServer,
) -> None:
    api = BasicDlboardAPI(base_url=backend_server.url)
    project = api.get_or_create_project("beating")
    experiment = api.create_experiment(models.NewExperiment(project_id=project.id))
    alive = api.create_run(models.NewRun(experiment_id=experiment.id))

    assert api.heartbeat(alive.id)
    seen = backend_server.store.get_run(alive.id)
    assert seen is not None
    assert seen.last_seen_at is not None
    assert not api.heartbeat(alive.id + 1000)


def test_a_run_launched_with_tags_is_found_by_them(backend_server: BackendServer) -> None:
    with run.init("plain", "tagged", server_url=backend_server.url, tags=["Baseline"]) as started:
        pass
    _stop(started)

    assert started.run_id is not None
    assert backend_server.store.run_tags(frozenset({started.run_id})) == {started.run_id: ["baseline"]}
