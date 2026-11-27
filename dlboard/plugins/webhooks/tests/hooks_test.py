"""What a webhook may post to, what an owner's save is checked for, and which event a run's end is."""

from __future__ import annotations

import shutil
import subprocess
import threading
import uuid
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import HttpUrl
from werkzeug.serving import make_server

from dlboard import models
from dlboard.plugins.webhooks._hooks import (
    Failure,
    Named,
    ProjectWebhooks,
    RefusedError,
    RunSummary,
    Signature,
    Webhook,
    WebhookEvent,
    WebhookPayload,
    check_url,
    send,
)
from dlboard.plugins.webhooks._tab import checked
from dlboard.serve import SettingWrite

if TYPE_CHECKING:
    from pathlib import Path
    from wsgiref.types import StartResponse, WSGIEnvironment


def _hooks(*urls: str) -> dict[str, Any]:
    made = [
        Webhook(url=HttpUrl(u), events=frozenset(WebhookEvent), link_base=HttpUrl("https://d/"), created_by=1)
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


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (500, Failure.RETRY),
        (503, Failure.RETRY),
        (408, Failure.RETRY),
        (429, Failure.RETRY),
        (404, Failure.GONE),
        (410, Failure.GONE),
        (400, Failure.REJECTED),
        (403, Failure.REJECTED),
        (302, Failure.REJECTED),  # redirects are not followed
    ],
)
def test_what_a_receivers_answer_makes_of_a_delivery(status: int, failure: Failure) -> None:
    assert Failure.of(status) is failure


@given(secret=st.text(min_size=1, max_size=8), body=st.binary(max_size=16), at=st.integers(0, 2**40))
def test_a_signature_reads_back_as_the_header_says_it(secret: str, body: bytes, at: int) -> None:
    signed = Signature.of(secret, body, at)
    assert Signature.parse(signed.header) == signed


def _answer(_environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
    start_response("204 No Content", [])
    return []


@pytest.mark.skipif(shutil.which("openssl") is None, reason="makes its certificate with openssl")
@pytest.mark.parametrize(("named", "lands"), [("localhost", True), ("other.example", False)])
def test_a_post_over_https_goes_to_the_checked_address_and_checks_the_certificate_against_the_urls_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str, lands: bool
) -> None:
    """Connected to by its address, the receiver is still known by its host's name: a certificate for another isn't it."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            *("openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"),
            *("-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", f"/CN={named}"),
            *("-addext", f"subjectAltName=DNS:{named}"),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))  # trusted as the system's CAs are
    monkeypatch.setenv("DLBOARD_WEBHOOK_ALLOWED_HOSTS", "localhost")
    server = make_server("127.0.0.1", 0, _answer, ssl_context=(str(cert), str(key)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    hook = Webhook(
        url=HttpUrl(f"https://localhost:{server.server_port}/hook"),
        events=frozenset(WebhookEvent),
        link_base=HttpUrl("https://d/"),
        created_by=1,
    )
    payload = WebhookPayload(
        event=WebhookEvent.FINISHED,
        text="r finished",
        project=Named(id=1, name="p"),
        experiment=Named(id=1, name=None),
        run=RunSummary(
            id=1,
            name=None,
            status=None,
            created_at=pendulum.now("UTC"),
            ended_at=None,
            done_at=pendulum.now("UTC"),
            duration_s=None,
        ),
        url=HttpUrl("https://d/experiment/1?run=1"),
    )
    try:
        if lands:
            send(hook, payload, uuid.uuid4())
        else:
            with pytest.raises(RefusedError, match="didn't answer"):
                send(hook, payload, uuid.uuid4())
    finally:
        server.shutdown()
        server.server_close()
