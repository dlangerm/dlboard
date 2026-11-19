"""End-to-end: a run token, minted for one run, is all a job needs to log to it, and is good for nothing else (#718)."""

from __future__ import annotations

import atexit
from http import HTTPStatus
from typing import TYPE_CHECKING, TypeAlias

import pendulum
import pytest
import requests
from pydantic import SecretStr

from dlboard import models
from dlboard.client import run
from dlboard.client._rest_api import BasicDlboardAPI

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.conftest import BackendServer

# Spawning the shipping processes re-imports this test session's heavy modules in each, routinely past the
# slow-startup warning's threshold on a test box -- expected here, not a finding.
pytestmark = pytest.mark.filterwarnings("ignore:dlboard's logging worker processes took")


def _two_runs(owner: BasicDlboardAPI) -> tuple[models.Run, models.Run]:
    project = owner.get_or_create_project("cluster")
    experiment = owner.get_or_create_experiment(project.id, "sweep")
    return (
        owner.create_run(models.NewRun(experiment_id=experiment.id)),
        owner.create_run(models.NewRun(experiment_id=experiment.id)),
    )


def test_a_job_with_a_run_token_logs_to_its_run_without_naming_it(
    backend_server: BackendServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = BasicDlboardAPI(backend_server.url)
    mine, _ = _two_runs(owner)
    monkeypatch.setenv("DLBOARD_API_KEY", owner.run_token(mine.id).token)

    with run.init("named in the job script", hparams={"lr": 0.1}, server_url=backend_server.url) as started:
        started.log_metrics({"loss": 0.5}, step=0)
    atexit.unregister(started._exit_hook)

    store = backend_server.store
    assert started.run_id == mine.id
    assert list(store.fetch_metrics(mine.experiment_id, run_ids=frozenset({mine.id})).step) == [0]
    stored = store.get_run(mine.id)
    assert stored is not None
    assert stored.status == models.RunStatus.FINISHED
    assert [project.name for project in store.get_projects()] == ["cluster"]


def test_a_submit_script_mints_a_token_for_a_new_run_from_python(backend_server: BackendServer) -> None:
    minted = run.run_token("cluster", "sweep", name="job-1", server_url=backend_server.url)

    job = BasicDlboardAPI(backend_server.url, api_key=SecretStr(minted.token))
    stored = backend_server.store.get_run(minted.run_id)
    assert stored is not None
    experiment = backend_server.store.get_experiment(stored.experiment_id)
    assert experiment is not None
    assert (job.whoami().run_id, stored.name, experiment.name) == (minted.run_id, "job-1", "sweep")
    assert minted.expires_at > pendulum.now(pendulum.UTC)


def _log_to(api: BasicDlboardAPI, other: models.Run) -> object:
    metrics = models.LoggedMetrics(
        experiment_id=other.experiment_id,
        run_id=other.id,
        step=0,
        metrics={"loss": 1.0},
        timestamp_utc=pendulum.now(pendulum.UTC),
    )
    return api.log_metric_batch([metrics])


_Attempt: TypeAlias = "Callable[[BasicDlboardAPI, models.Run], object]"

_ATTEMPTS: list[tuple[_Attempt, HTTPStatus]] = [
    (_log_to, HTTPStatus.FORBIDDEN),
    (lambda api, other: api.mark_run(other.id, keep=True), HTTPStatus.NOT_FOUND),
    (lambda api, other: api.get_run(other.id), HTTPStatus.NOT_FOUND),
    (lambda api, other: api.runs(other.experiment_id), HTTPStatus.FORBIDDEN),
    (lambda api, _: api.get_or_create_project("elsewhere"), HTTPStatus.FORBIDDEN),
    (lambda api, other: api.run_token(other.id), HTTPStatus.FORBIDDEN),
]
"""What a job holding a run token for one run tries with another (or with none), and what it is refused with."""


@pytest.mark.parametrize(
    ("attempt", "refused_with"),
    _ATTEMPTS,
    ids=[
        "log to another run",
        "keep another run",
        "see another run",
        "read",
        "make a project",
        "mint a token",
    ],
)
def test_a_run_token_reaches_nothing_but_its_own_run(
    backend_server: BackendServer,
    attempt: _Attempt,
    refused_with: HTTPStatus,
) -> None:
    owner = BasicDlboardAPI(backend_server.url)
    mine, other = _two_runs(owner)
    job = BasicDlboardAPI(backend_server.url, api_key=SecretStr(owner.run_token(mine.id).token))

    with pytest.raises(requests.HTTPError) as refused:
        attempt(job, other)

    assert refused.value.response is not None
    assert refused.value.response.status_code == refused_with
    assert job.whoami().run_id == mine.id
    assert (
        requests.get(backend_server.url, headers=job._headers, timeout=10).status_code == HTTPStatus.FORBIDDEN
    )
