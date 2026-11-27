"""
Webhooks' tasks, end to end against a real receiver: a run that ends is posted, signed, once, to each webhook that asks
for how it ended, as it was when it ended; a webhook whose deliveries keep failing, or whose receiver is gone, is turned
off; a post the receiver refuses is dropped.
"""

from __future__ import annotations

import hmac
import threading
import uuid
from contextlib import contextmanager
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
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    ProjectWebhooks,
    Signature,
    Webhook,
    WebhookEvent,
    WebhookKey,
    WebhookPayload,
)
from dlboard.plugins.webhooks._tasks import WebhookTask, WebhookWork
from dlboard.serve import SQLStoreBase, get_system_data_store
from dlboard.serve import app as build_app
from dlboard.serve._custodian import get_system_task_store

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path
    from wsgiref.types import StartResponse, WSGIEnvironment

    from dash import Dash

    from dlboard.models import DataStore, PluginProtocol


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


@contextmanager
def _built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugins: list[PluginProtocol]) -> Generator[Dash]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    built = build_app([*plugins, *LOCAL_AUTH, *BUILTIN_BACKEND])
    try:
        yield built
    finally:
        dispose_stores(built)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dash]:
    with _built(tmp_path, monkeypatch, [sqlite, filesystem, webhooks_plugin]) as built:
        yield built


@pytest.fixture
def store(app: Dash) -> SQLStoreBase[Any]:
    found = get_system_data_store(app)
    assert isinstance(found, SQLStoreBase)
    return found


def _with_hooks(store: DataStore[...], *hooks: Webhook) -> tuple[int, int]:
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
        link_base=HttpUrl("https://dlboard.example/"),
        created_by=1,
        created_at=pendulum.now("UTC").subtract(minutes=1),
    )


def _stored_hook(store: SQLStoreBase[Any], project_id: int) -> Webhook:
    found = store.get_scoped_settings(WebhookKey.HOOKS, project_id).project
    assert found is not None
    (hook,) = ProjectWebhooks.model_validate(found.value).hooks
    return hook


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
    signed = Signature.parse(headers[SIGNATURE_HEADER])
    assert hmac.compare_digest(signed.digest, Signature.of(asks.secret, body, signed.at).digest)
    assert headers[EVENT_HEADER] == WebhookEvent.FINISHED
    assert uuid.UUID(headers[DELIVERY_HEADER])
    posted = WebhookPayload.model_validate_json(body)
    assert posted.run.id == finished.id
    assert (posted.project.id, posted.experiment.name) == (project_id, "sweep")
    assert str(posted.url) == f"https://dlboard.example/experiment/{experiment_id}?run={finished.id}"
    assert posted.text.startswith("lr-3e-4 finished")


def test_a_run_given_up_as_lost_is_posted_as_crashed_though_it_was_heard_from_before_the_post(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    """What is posted is the run as it ended, made when its end was found: not as it is by the time it is posted."""
    _, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.CRASHED))
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.mark_lost_runs(pendulum.now("UTC").add(seconds=1), limit=10)
    run_task(app, WebhookTask.FIND)
    store.heartbeat(run.id)  # running again

    run_task(app, WebhookTask.DELIVER)

    ((headers, body),) = receiver.received
    assert headers[EVENT_HEADER] == WebhookEvent.CRASHED
    assert WebhookPayload.model_validate_json(body).run.status is models.RunStatus.UNKNOWN


def test_a_lost_run_heard_from_again_before_its_end_is_found_is_not_posted(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    """Its end was undone: it is running again, and is posted when it ends for good."""
    _, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.CRASHED, WebhookEvent.FINISHED))
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.mark_lost_runs(pendulum.now("UTC").add(seconds=1), limit=10)
    store.heartbeat(run.id)
    _announce(app)
    assert receiver.received == []

    store.finish_run(run.id, models.RunStatus.FINISHED, pendulum.now("UTC"))
    _announce(app)

    ((headers, _),) = receiver.received
    assert headers[EVENT_HEADER] == WebhookEvent.FINISHED


def test_every_end_is_found_when_the_plugin_is_listed_before_its_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store hears which work a run's end queues once every plugin has plugged, in whatever order."""
    with _built(tmp_path, monkeypatch, [webhooks_plugin, sqlite, filesystem]) as built:
        store = get_system_data_store(built)
        _, experiment_id = _with_hooks(store)
        run = store.create_run(models.NewRun(experiment_id=experiment_id))
        store.finish_run(run.id, models.RunStatus.FINISHED, pendulum.now("UTC"))

        assert get_system_task_store(built).count_work(WebhookWork.RUN_END) == 1


@pytest.fixture
def one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_WEBHOOK_ATTEMPTS", "1")


@pytest.mark.usefixtures("one_attempt")
@pytest.mark.parametrize(
    ("status", "turned_off"),
    [
        pytest.param(
            500, "Turned off after 1 failed deliveries: 127.0.0.1 answered 500", id="failing-for-good"
        ),
        pytest.param(410, "Turned off: 127.0.0.1 answered 410", id="gone"),
        pytest.param(400, None, id="refused-is-dropped-and-the-webhook-kept"),
    ],
)
def test_what_a_failed_delivery_does_to_its_webhook(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver, status: int, turned_off: str | None
) -> None:
    """Each ends its delivery: none is left to try again."""
    receiver.status = status
    project_id, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.FAILED))
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.finish_run(run.id, models.RunStatus.FAILED, pendulum.now("UTC"))

    _announce(app)

    assert _stored_hook(store, project_id).turned_off == turned_off
    assert get_system_task_store(app).count_work(WebhookWork.DELIVERY) == 0


def test_a_webhook_that_fails_is_posted_once_a_pass_however_many_deliveries_it_has(
    app: Dash, store: SQLStoreBase[Any], receiver: _Receiver
) -> None:
    receiver.status = 503
    _, experiment_id = _with_hooks(store, _hook(receiver.url, WebhookEvent.FINISHED))
    for _ in range(3):
        run = store.create_run(models.NewRun(experiment_id=experiment_id))
        store.finish_run(run.id, models.RunStatus.FINISHED, pendulum.now("UTC"))

    _announce(app)

    assert len(receiver.received) == 1
    assert get_system_task_store(app).count_work(WebhookWork.DELIVERY) == 3


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
