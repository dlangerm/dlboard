"""What a webhook may post to, what an owner's save is checked for, and which event a run's end is."""

from __future__ import annotations

from typing import Any

import pendulum
import pytest
from pydantic import HttpUrl

from dlboard import models
from dlboard.plugins.webhooks._hooks import ProjectWebhooks, RefusedError, Webhook, WebhookEvent, check_url
from dlboard.plugins.webhooks._tab import checked
from dlboard.serve import SettingWrite


def _hooks(*urls: str) -> dict[str, Any]:
    made = [
        Webhook(url=HttpUrl(u), events=frozenset(WebhookEvent), link_base="https://d", created_by=1)
        for u in urls
    ]
    return ProjectWebhooks(hooks=tuple(made)).model_dump(mode="json")


def _write(
    value: dict[str, Any] | None, stored: dict[str, Any] | None = None, experiment_id: int | None = None
) -> SettingWrite:
    return SettingWrite(
        project_id=1, experiment_id=experiment_id, value=value, stored=stored, admin=False, actor_id=1
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost:8050/api",  # a name for this machine
        "http://[::1]/",
        "http://10.1.2.3/",
        "http://192.168.0.10/",
        "http://169.254.169.254/latest/meta-data",  # a cloud's metadata service
        "http://100.64.0.1/",  # carrier-grade NAT, a tailnet's
    ],
)
def test_a_webhook_may_not_post_to_an_address_that_isnt_public(url: str) -> None:
    with pytest.raises(RefusedError, match="isn't public"):
        check_url(HttpUrl(url))


def test_an_admin_lets_a_host_through_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_WEBHOOK_ALLOWED_HOSTS", "chat.internal, 10.1.2.3")
    check_url(HttpUrl("http://10.1.2.3/hook"))
    check_url(HttpUrl("https://93.184.215.14/hook"))  # public: needs no one's say


@pytest.mark.parametrize(
    ("write", "refused"),
    [
        pytest.param(_write(_hooks("https://93.184.215.14/")), None, id="a-public-url"),
        pytest.param(_write(_hooks("http://10.1.2.3/")), ValueError, id="a-new-private-url"),
        pytest.param(
            _write(_hooks("http://10.1.2.3/", "https://93.184.215.14/"), stored=_hooks("http://10.1.2.3/")),
            None,
            id="one-it-had-already-is-not-resolved-again",
        ),
        pytest.param(
            _write(_hooks(*(f"https://93.184.215.{i}/" for i in range(11)))), ValueError, id="too-many"
        ),
        pytest.param(_write(None, experiment_id=3), PermissionError, id="an-experiments"),
        pytest.param(_write(None), None, id="deleting-them-all"),
    ],
)
def test_an_owners_save_is_checked(write: SettingWrite, refused: type[Exception] | None) -> None:
    if refused is None:
        assert checked(write) == write.value
    else:
        with pytest.raises(refused):
            checked(write)


def _run(status: models.RunStatus, *, lost: bool = False) -> models.Run:
    now = pendulum.now("UTC")
    return models.Run(id=1, experiment_id=1, status=status, lost_at=now if lost else None, done_at=now)


@pytest.mark.parametrize(
    ("run", "event"),
    [
        (_run(models.RunStatus.FINISHED), WebhookEvent.FINISHED),
        (_run(models.RunStatus.FAILED), WebhookEvent.FAILED),
        (_run(models.RunStatus.UNKNOWN, lost=True), WebhookEvent.CRASHED),
        (_run(models.RunStatus.UNKNOWN), WebhookEvent.ENDED),
        (_run(models.RunStatus.RUNNING), None),
    ],
)
def test_how_a_run_ended_is_its_event(run: models.Run, event: WebhookEvent | None) -> None:
    assert WebhookEvent.of(run) is event
