"""End-to-end: a run `dlboard.client.run.init` started (real shipping processes) logging to a live dlboard backend."""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.client import run

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
