"""
Webhooks' tasks, end to end against a real receiver: a run that ends is posted, signed, once, to each webhook that asks
for how it ended; a webhook whose deliveries keep failing is turned off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from pydantic import HttpUrl
from werkzeug.serving import make_server
from werkzeug.wrappers import Request

from dlboard import models
from dlboard.conftest import dispose_stores, run_task
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins import webhooks as webhooks_plugin
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.plugins.webhooks._hooks import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    ProjectWebhooks,
    Webhook,
    WebhookEvent,
    WebhookKey,
)
from dlboard.plugins.webhooks._tasks import WebhookTask
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from wsgiref.types import StartResponse, WSGIEnvironment

    from dash import Dash


@dataclass
class _Receiver:
    """A WSGI app that takes posts, answering each with `status`, and keeps them."""

    url: str = ""
    status: int = 200
    received: list[tuple[dict[str, str], bytes]] = field(default_factory=list)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        posted = Request(environ)
        self.received.append((dict(posted.headers.items()), posted.get_data()))
        start_response(f"{self.status} Answer", [])
        return [b""]


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Receiver]:
    """A receiver on this machine: which a webhook reaches only with an admin's say (`DLBOARD_WEBHOOK_ALLOWED_HOSTS`)."""
    monkeypatch.setenv("DLBOARD_WEBHOOK_ALLOWED_HOSTS", "127.0.0.1")
    app = _Receiver()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    app.url = f"http://127.0.0.1:{server.server_port}/hook"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app
    server.shutdown()
    server.server_close()


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([sqlite, filesystem, webhooks_plugin, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield built
    dispose_stores(built)


@pytest.fixture
def store(app: Dash) -> SQLStoreBase[Any]:
    found = get_system_data_store(app)
    assert isinstance(found, SQLStoreBase)
    return found


def _with_hooks(store: SQLStoreBase[Any], *hooks: Webhook) -> tuple[int, int]:
    """A project with `hooks`, and its experiment."""
    project = store.create_project(models.NewProject(name="vision", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="sweep"))
    me = store.get_or_create_user(models.Principal.unverified("me"))
    value = ProjectWebhooks(hooks=hooks).model_dump(mode="json")
    store.put_scoped_setting(WebhookKey.HOOKS, project.id, None, value, me)
    return project.id, experiment.id


def _hook(url: str, *events: WebhookEvent) -> Webhook:
    return Webhook(
        url=HttpUrl(url),
        events=frozenset(events),
        link_base="https://dlboard.example",
        created_by=1,
        created_at=pendulum.now("UTC").subtract(minutes=1),
    )


def _announce(app: Dash) -> None:
    run_task(app, WebhookTask.FIND)
    run_task(app, WebhookTask.DELIVER)


def test_a_run_that_ends_is_posted_signed_once_to_each_webhook_that_asks_for_how(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    asks = _hook(receiver.url, WebhookEvent.FINISHED, WebhookEvent.CRASHED)
    only_failures = _hook(receiver.url + "?failures", WebhookEvent.FAILED)
    project_id, experiment_id = _with_hooks(store, asks, only_failures)
    finished = store.create_run(models.NewRun(experiment_id=experiment_id, name="lr-3e-4"))
    store.finish_run(finished.id, models.RunStatus.FINISHED, pendulum.now("UTC"))

    _announce(app)
    # Lightning finalizing again.
    store.finish_run(finished.id, models.RunStatus.FINISHED, pendulum.now("UTC"))
    _announce(app)

    ((headers, body),) = receiver.received
    timestamp, signature = (part.split("=", 1)[1] for part in headers[SIGNATURE_HEADER].split(","))
    expected = hmac.new(asks.secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    assert headers[EVENT_HEADER] == WebhookEvent.FINISHED
    posted = json.loads(body)
    assert posted["run"]["id"] == finished.id
    assert (posted["project"]["id"], posted["experiment"]["name"]) == (project_id, "sweep")
    assert posted["url"] == f"https://dlboard.example/experiment/{experiment_id}?run={finished.id}"
    assert posted["text"].startswith("lr-3e-4 finished")


def test_a_run_given_up_as_lost_is_posted_as_crashed(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    _, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.CRASHED))
    store.create_run(models.NewRun(experiment_id=experiment_id))
    store.mark_lost_runs(pendulum.now("UTC").add(seconds=1), limit=10)

    _announce(app)

    ((headers, _),) = receiver.received
    assert headers[EVENT_HEADER] == WebhookEvent.CRASHED


def test_a_webhook_whose_deliveries_keep_failing_is_turned_off_saying_why(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver.status = 500
    monkeypatch.setenv("DLBOARD_WEBHOOK_ATTEMPTS", "1")
    project_id, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.FAILED))
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.finish_run(run.id, models.RunStatus.FAILED, pendulum.now("UTC"))

    _announce(app)

    found = store.get_scoped_settings(WebhookKey.HOOKS, project_id).project
    assert found is not None
    (hook,) = ProjectWebhooks.model_validate(found.value).hooks
    assert hook.turned_off is not None
    assert "answered 500" in hook.turned_off


def test_a_run_that_ended_before_its_webhook_was_made_is_not_posted(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    project_id, experiment_id = _with_hooks(store)
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.finish_run(run.id, models.RunStatus.FINISHED, pendulum.now("UTC"))
    _announce(app)  # no webhook yet: nothing to start from
    made_after = _hook(receiver.url, WebhookEvent.FINISHED).model_copy(
        update={"created_at": pendulum.now("UTC")}
    )
    me = store.get_or_create_user(models.Principal.unverified("me"))
    value = ProjectWebhooks(hooks=(made_after,)).model_dump(mode="json")
    store.put_scoped_setting(WebhookKey.HOOKS, project_id, None, value, me)

    _announce(app)

    assert receiver.received == []
