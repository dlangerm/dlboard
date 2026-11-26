"""
A project's webhooks, what one is sent when a run ends, and the one way it is sent.

**Sending** never reaches anything a project owner shouldn't make the server reach: the URL's host is resolved once, every
address it has must be a public one (`is_global`: not loopback, private, link-local, carrier-grade NAT, ...) unless an
admin listed the host in `DLBOARD_WEBHOOK_ALLOWED_HOSTS`, and the request goes to that resolved address (the certificate
checked against the host's name), so a DNS answer that changes between the check and the request (rebinding) can't
redirect it. Redirects are not followed, and the request has short timeouts.

**Signing**: `X-Dlboard-Signature: t=<unix time>,v1=<hex>` is HMAC-SHA256, keyed by the webhook's secret, of
`<unix time>.<body>`: a receiver checks it, and that the time is recent, to know the request is dlboard's and not a replay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import ssl
import time
from ipaddress import ip_address
from typing import TYPE_CHECKING, Annotated, Any, Final

import pendulum
import urllib3
from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    HttpUrl,
    PositiveFloat,
    PositiveInt,
    field_serializer,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._version import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

SIGNATURE_HEADER: Final = "X-Dlboard-Signature"
EVENT_HEADER: Final = "X-Dlboard-Event"


class WebhookKey(StrEnum):
    """The plugin's scoped setting."""

    HOOKS = "webhooks"


class WebhookEvent(StrEnum):
    """How a run ended: what a webhook is sent for. Stored, so a value is never renamed or removed."""

    FINISHED = "run.finished"
    FAILED = "run.failed"
    CRASHED = "run.crashed"
    """Given up as lost: running, and not heard from for long (its process killed, its node gone)."""
    ENDED = "run.ended"
    """Ended without its client saying how."""

    @classmethod
    def of(cls, run: models.Run) -> WebhookEvent | None:
        """How `run` ended, or `None` if it hasn't."""
        match run.status:
            case models.RunStatus.FINISHED:
                return cls.FINISHED
            case models.RunStatus.FAILED:
                return cls.FAILED
            case models.RunStatus.UNKNOWN:
                return cls.CRASHED if run.lost_at is not None else cls.ENDED
            case models.RunStatus.RUNNING | None:
                return None


class WebhookSettings(BaseSettings):
    """`DLBOARD_WEBHOOK_*`: the server's own say over webhooks."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_WEBHOOK_")

    allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset()
    """Comma-separated hosts a webhook may reach though they resolve to a non-public address (an internal chat server)."""
    max_per_project: PositiveInt = 10
    timeout_s: PositiveFloat = 5.0
    """How long a delivery waits to connect, and then for the answer."""
    attempts: PositiveInt = 10
    """Tries a delivery gets (a minute apart, doubling) before its webhook is turned off: about eight and a half hours."""
    interval_s: PositiveInt = 30
    """How often the server looks for runs that ended."""

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return {v.strip().lower() for v in value.split(",") if v.strip()} if isinstance(value, str) else value


class Webhook(BaseModel, frozen=True, extra="forbid"):
    """One webhook of a project: where it posts, for which events, and the secret its posts are signed with."""

    id: str = Field(default_factory=lambda: secrets.token_hex(8))
    url: HttpUrl
    events: frozenset[WebhookEvent]
    secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    """Shown to its owner once, when it is made; never drawn again, nor logged."""
    link_base: str
    """The server's address as its owner reached it (with any URL prefix): what links to a run start with."""
    created_by: int
    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """A run that ended before this is not announced."""
    turned_off: str | None = None
    """Why the server turned it off (its deliveries kept failing), until its owner turns it on again."""

    @field_serializer("events")
    def _in_order(self, events: frozenset[WebhookEvent]) -> list[WebhookEvent]:
        """Stored the same way each time: a set has no order of its own."""
        return sorted(events)


class ProjectWebhooks(BaseModel, frozen=True, extra="forbid"):
    """A project's webhooks: the setting's value."""

    hooks: tuple[Webhook, ...] = ()

    def find(self, hook_id: str) -> Webhook | None:
        return next((hook for hook in self.hooks if hook.id == hook_id), None)

    def replaced(self, hook_id: str, hook: Webhook | None) -> ProjectWebhooks:
        """These webhooks, with `hook_id`'s replaced by `hook` (`None`: removed)."""
        kept = (hook if h.id == hook_id else h for h in self.hooks)
        return ProjectWebhooks(hooks=tuple(h for h in kept if h is not None))


class RefusedError(ValueError):
    """A URL the server won't post to, or a post that didn't land: why, in words for the webhook's owner."""


def _addresses(url: HttpUrl) -> list[str]:
    """The addresses `url`'s host resolves to that the server may post to: all of them must be, or none is."""
    host, port = (
        (url.host or "").strip("[]"),
        url.port or (443 if url.scheme == "https" else 80),
    )  # an IPv6's brackets
    try:
        found = sorted({str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as error:
        msg = f"{host} doesn't resolve ({error})"
        raise RefusedError(msg) from error
    if host.lower() in WebhookSettings().allowed_hosts:
        return found
    private = [a for a in found if not ip_address(a.split("%")[0]).is_global]
    if private:
        msg = f"{host} resolves to an address that isn't public ({private[0]}): an admin can allow it"
        raise RefusedError(msg)
    return found


def check_url(url: HttpUrl) -> None:
    """Refuse a URL a webhook may not post to, as it is saved: the same check every delivery makes again."""
    if url.scheme not in {"http", "https"}:
        msg = "A webhook posts to an http:// or https:// URL"
        raise RefusedError(msg)
    _addresses(url)


def _signature(secret: str, body: bytes, at: int) -> str:
    digest = hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={at},v1={digest}"


def send(hook: Webhook, event: WebhookEvent, body: dict[str, Any]) -> None:
    """Post `body` to `hook`, signed, to an address `_addresses` allows; `RefusedError` saying why if it didn't land."""
    settings, url, host = WebhookSettings(), hook.url, hook.url.host or ""
    address = _addresses(url)[0]
    port = url.port or (443 if url.scheme == "https" else 80)
    timeout = urllib3.Timeout(connect=settings.timeout_s, read=settings.timeout_s)
    pool = (
        urllib3.HTTPSConnectionPool(
            address,
            port,
            timeout=timeout,
            ssl_context=ssl.create_default_context(),
            server_hostname=host.strip("[]"),
            assert_hostname=host.strip("[]"),
        )
        if url.scheme == "https"
        else urllib3.HTTPConnectionPool(address, port, timeout=timeout)
    )
    payload = json.dumps(body).encode()
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    headers: dict[str, str] = {
        # The port as the URL gave it (`HttpUrl` fills in the scheme's own when it gave none).
        "Host": host if port == {"https": 443, "http": 80}[url.scheme] else f"{host}:{port}",
        "Content-Type": "application/json",
        "User-Agent": f"dlboard/{__version__}",
        EVENT_HEADER: str(event),
        SIGNATURE_HEADER: _signature(hook.secret, payload, int(time.time())),
    }
    try:
        with pool:
            response = pool.urlopen(
                "POST", path, body=payload, headers=headers, retries=False, redirect=False
            )
    except urllib3.exceptions.HTTPError as error:
        msg = f"{url.host} didn't answer ({type(error).__name__})"
        raise RefusedError(msg) from error
    if not 200 <= response.status < 300:  # noqa: PLR2004 -- the 2xx range
        msg = f"{url.host} answered {response.status}"
        raise RefusedError(msg)


def body_for(
    event: WebhookEvent,
    run: models.Run,
    experiment: models.Experiment,
    project: models.Project,
    link_base: str,
) -> dict[str, Any]:
    """
    What a webhook is sent: the event, the project, experiment and run, and a link to the run.

    `text` says it in a sentence, which is all a Slack incoming webhook reads (it ignores the rest).
    """
    link = f"{link_base}/experiment/{experiment.id}?run={run.id}"
    name = run.name or f"Run {run.id}"
    took = (run.ended_at - run.created_at).total_seconds() if run.ended_at is not None else None
    how = {
        WebhookEvent.FINISHED: "finished",
        WebhookEvent.FAILED: "failed",
        WebhookEvent.CRASHED: "crashed (its process stopped being heard from)",
        WebhookEvent.ENDED: "ended without saying how",
    }[event]
    after = (
        f" after {pendulum.duration(seconds=round(took)).in_words()}"
        if took is not None and took >= 0
        else ""
    )
    where = f"{project.name} / {experiment.name or f'Experiment {experiment.id}'}"
    return {
        "event": event,
        "text": f"{name} {how}{after}, in {where}: {link}",
        "project": {"id": project.id, "name": project.name},
        "experiment": {"id": experiment.id, "name": experiment.name},
        "run": {
            "id": run.id,
            "name": run.name,
            "status": run.status,
            "created_at": run.created_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
            "duration_s": took,
        },
        "url": link,
    }


def subscribed(hooks: Sequence[Webhook], event: WebhookEvent, run: models.Run) -> list[Webhook]:
    """The webhooks that are on, ask for `event`, and were made before `run` ended."""
    done_at = run.done_at
    return [
        h
        for h in hooks
        if h.turned_off is None and event in h.events and done_at is not None and done_at >= h.created_at
    ]
