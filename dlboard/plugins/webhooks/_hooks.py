"""
A project's webhooks, what one is sent when a run ends, and the one way it is sent.

**Sending** never reaches anything a project owner shouldn't make the server reach: the URL's host is resolved once, every
address it has must be a public one (`is_global`: not loopback, private, link-local, carrier-grade NAT, ...) unless an
admin listed the host in `DLBOARD_WEBHOOK_ALLOWED_HOSTS`, and the request goes to that resolved address (the certificate
checked against the host's name, `_PinnedAdapter`), so a DNS answer that changes between the check and the request
(rebinding) can't redirect it. Redirects are not followed, no proxy is used, and the request has short timeouts.

**Signing**: `X-Dlboard-Signature: t=<unix time>,v1=<hex>` is HMAC-SHA256, keyed by the webhook's secret, of
`<unix time>.<body>`: a receiver checks it, and that the time is recent, to know the request is dlboard's and not a replay.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import socket
import ssl
import time
import uuid
from http import HTTPStatus
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Annotated, Any, Final, Self, override
from urllib.parse import urljoin, urlsplit

import pendulum
import requests
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
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._version import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

SIGNATURE_HEADER: Final = "X-Dlboard-Signature"
EVENT_HEADER: Final = "X-Dlboard-Event"
DELIVERY_HEADER: Final = "X-Dlboard-Delivery"
_SIGNATURE: Final = re.compile(r"t=(?P<at>\d+),v1=(?P<digest>[0-9a-f]{64})")
_DELIVERY_IDS: Final = uuid.UUID("6f1c2b7e-3d1a-4c55-9a51-0d2f1e7b9c40")
"""The namespace a delivery's id is made in from its key: the same delivery, retried, has the same id."""


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

    @property
    def label(self) -> str:
        """What the Webhooks tab calls it."""
        match self:
            case WebhookEvent.FINISHED:
                return "Finished"
            case WebhookEvent.FAILED:
                return "Failed"
            case WebhookEvent.CRASHED:
                return "Crashed"
            case WebhookEvent.ENDED:
                return "Ended without saying how"

    @property
    def verb(self) -> str:
        """What a run did, in the sentence a post's `text` is."""
        match self:
            case WebhookEvent.FINISHED:
                return "finished"
            case WebhookEvent.FAILED:
                return "failed"
            case WebhookEvent.CRASHED:
                return "crashed (its process stopped being heard from)"
            case WebhookEvent.ENDED:
                return "ended without saying how"


class WebhookSettings(BaseSettings):
    """`DLBOARD_WEBHOOK_*`: the server's own say over webhooks."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_WEBHOOK_")

    allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset()
    """Comma-separated hosts a webhook may reach though they resolve to a non-public address (an internal chat server)."""
    public_url: HttpUrl | None = None
    """Where people reach the server (scheme and host), for the links a post has; unset, where its owner reached it."""
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
    link_base: HttpUrl
    """The server's address, with any URL prefix and a closing `/`: what the links in what it posts start with."""
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


class ProjectRef(BaseModel, frozen=True, extra="forbid"):
    id: int
    name: str


class ExperimentRef(BaseModel, frozen=True, extra="forbid"):
    id: int
    name: str | None


class RunSummary(BaseModel, frozen=True, extra="forbid"):
    """The run, as it was when it ended."""

    id: int
    name: str | None
    status: models.RunStatus | None
    created_at: AwareDatetime
    ended_at: AwareDatetime | None
    duration_s: float | None


class WebhookPayload(BaseModel, frozen=True, extra="forbid"):
    """
    What a webhook is sent: the event, the project, experiment and run, and a link to the run.

    `text` says it in a sentence, which is all a Slack incoming webhook reads (it ignores the rest). Made once, when the
    run's end is found, so every try of a delivery posts the same body, describing the run as it ended.
    """

    event: WebhookEvent
    text: str
    project: ProjectRef
    experiment: ExperimentRef
    run: RunSummary
    url: HttpUrl

    @classmethod
    def of(
        cls,
        event: WebhookEvent,
        run: models.Run,
        experiment: models.Experiment,
        project: models.Project,
        link_base: HttpUrl,
    ) -> Self:
        link = HttpUrl(urljoin(str(link_base), f"experiment/{experiment.id}?run={run.id}"))
        took = (run.ended_at - run.created_at).total_seconds() if run.ended_at is not None else None
        after = (
            f" after {pendulum.duration(seconds=round(took)).in_words()}"
            if took is not None and took >= 0
            else ""
        )
        where = f"{project.name} / {experiment.name or f'Experiment {experiment.id}'}"
        return cls(
            event=event,
            text=f"{run.name or f'Run {run.id}'} {event.verb}{after}, in {where}: {link}",
            project=ProjectRef(id=project.id, name=project.name),
            experiment=ExperimentRef(id=experiment.id, name=experiment.name),
            run=RunSummary(
                id=run.id,
                name=run.name,
                status=run.status,
                created_at=run.created_at,
                ended_at=run.ended_at,
                duration_s=took,
            ),
            url=link,
        )


class Signature(BaseModel, frozen=True, extra="forbid"):
    """`X-Dlboard-Signature`: when a post was signed, and the HMAC-SHA256 of `<at>.<body>` under the webhook's secret."""

    at: int
    digest: str

    @classmethod
    def of(cls, secret: str, body: bytes, at: int) -> Self:
        return cls(
            at=at, digest=hmac.new(secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
        )

    @classmethod
    def parse(cls, header: str) -> Self:
        """The signature a header says, as a receiver reads it; `ValueError` if it isn't one."""
        found = _SIGNATURE.fullmatch(header)
        if found is None:
            msg = f"Not a {SIGNATURE_HEADER}: {header!r}"
            raise ValueError(msg)
        return cls(at=int(found["at"]), digest=found["digest"])

    @property
    def header(self) -> str:
        return f"t={self.at},v1={self.digest}"


class Failure(StrEnum):
    """Why a post didn't land, and so what becomes of its delivery."""

    RETRY = "retry"
    """Nothing, or a 408, 429 or 5xx, answered: it may land later."""
    GONE = "gone"
    """404 or 410: the endpoint was deleted (as a removed Slack webhook answers), so its webhook is turned off at once."""
    REJECTED = "rejected"
    """Another 4xx: the receiver refused this post, which would be refused again; it is dropped, the webhook kept on."""

    @classmethod
    def of(cls, status: int) -> Failure:
        match status:
            case HTTPStatus.NOT_FOUND | HTTPStatus.GONE:
                return cls.GONE
            case HTTPStatus.REQUEST_TIMEOUT | HTTPStatus.TOO_MANY_REQUESTS:
                return cls.RETRY
            case _ if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                return cls.RETRY
            case _:
                return cls.REJECTED


class RefusedError(ValueError):
    """A URL the server won't post to, or a post that didn't land: why, in words for the webhook's owner, and what then."""

    def __init__(self, why: str, failure: Failure = Failure.RETRY) -> None:
        super().__init__(why)
        self.failure = failure


def _address(url: HttpUrl) -> IPv4Address | IPv6Address:
    """The address the server posts to for `url`: its host's first, if every one it resolves to may be posted to."""
    host = urlsplit(str(url)).hostname or ""
    try:
        resolved = {
            ip_address(info[4][0]) for info in socket.getaddrinfo(host, url.port, type=socket.SOCK_STREAM)
        }
        # IPv4 first: an IPv4 and an IPv6 address don't compare with each other.
        found = sorted(resolved, key=lambda a: (a.version, a))
    except OSError as error:
        msg = f"{host} doesn't resolve ({error})"
        raise RefusedError(msg) from error
    private = [a for a in found if not a.is_global]
    if private and host not in WebhookSettings().allowed_hosts:
        msg = f"{host} resolves to an address that isn't public ({private[0]}): an admin can allow it"
        raise RefusedError(msg)
    return found[0]


def check_url(url: HttpUrl) -> None:
    """Refuse a URL a webhook may not post to, as it is saved: the same check every delivery makes again."""
    _address(url)


class _PinnedAdapter(HTTPAdapter):
    """Connects to whatever address the URL names, while TLS checks the certificate against `hostname`, the real host's."""

    def __init__(self, hostname: str) -> None:
        self._hostname = hostname  # before `super().__init__`, which makes the pool manager
        super().__init__(max_retries=0)

    @override
    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        super().init_poolmanager(
            connections,
            maxsize,
            block,
            server_hostname=self._hostname,
            assert_hostname=self._hostname,
            # The system's trusted CAs, beside the certifi bundle `requests` adds: an internal host's private CA works.
            ssl_context=ssl.create_default_context(),
            **pool_kwargs,
        )


def send(hook: Webhook, payload: WebhookPayload, delivery_id: uuid.UUID) -> None:
    """Post `payload` to `hook`, signed, at an address `_address` allows; `RefusedError` saying why if it didn't land."""
    address = _address(hook.url)
    target = parse_url(str(hook.url))
    pinned = target._replace(host=f"[{address}]" if address.version == 6 else str(address))  # noqa: PLR2004 -- IPv6
    body = payload.model_dump_json().encode()
    timeout = WebhookSettings().timeout_s
    with requests.Session() as session:
        session.trust_env = False  # a proxy would connect to the host by its name, undoing the address check
        session.mount(f"{target.scheme}://", _PinnedAdapter(urlsplit(str(hook.url)).hostname or ""))
        try:
            response = session.post(
                pinned.url,
                data=body,
                headers={
                    "Host": target.netloc or "",
                    "Content-Type": "application/json",
                    "User-Agent": f"dlboard/{__version__}",
                    EVENT_HEADER: payload.event,
                    DELIVERY_HEADER: str(delivery_id),
                    SIGNATURE_HEADER: Signature.of(hook.secret, body, int(time.time())).header,
                },
                timeout=(timeout, timeout),
                allow_redirects=False,
            )
        except requests.RequestException as error:
            msg = f"{target.host} didn't answer ({type(error).__name__})"
            raise RefusedError(msg) from error
    if not HTTPStatus.OK <= response.status_code < HTTPStatus.MULTIPLE_CHOICES:
        msg = f"{target.host} answered {response.status_code}"
        raise RefusedError(msg, Failure.of(response.status_code))


def delivery_id(key: str) -> uuid.UUID:
    """The id a delivery is posted with (`X-Dlboard-Delivery`): the same for each try, so a receiver can drop a repeat."""
    return uuid.uuid5(_DELIVERY_IDS, key)


def link_base(owner_reached: str, prefix: str) -> HttpUrl:
    """Where a new webhook's links start: the server's public URL if an admin set one, else where its owner reached it."""
    public = WebhookSettings().public_url
    return HttpUrl(urljoin(str(public) if public is not None else owner_reached, prefix))


def subscribed(hooks: Sequence[Webhook], event: WebhookEvent, run: models.Run) -> list[Webhook]:
    """The webhooks that are on, ask for `event`, and were made before `run` ended."""
    done_at = run.done_at
    return [
        h
        for h in hooks
        if h.turned_off is None and event in h.events and done_at is not None and done_at >= h.created_at
    ]
