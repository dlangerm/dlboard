"""
A project's webhooks, what one is sent when a run ends, and the one way it is sent.

**Sending** never reaches anything a project owner shouldn't make the server reach: the URL's host is resolved once, every
address it has must be a public one (`is_global`: not loopback, private, link-local, carrier-grade NAT, ...) unless an
admin listed the host in `DLBOARD_WEBHOOK_ALLOWED_HOSTS`, and the request goes to that resolved address (the certificate
checked against the host's name, `_PinnedAdapter`), so a DNS answer that changes between the check and the request
(rebinding) can't redirect it. Redirects are not followed, no proxy is used, the request has a deadline, and only the
answer's status is read.

**Signing**: `X-Dlboard-Signature: t=<unix time>,v1=<hex>` is HMAC-SHA256, keyed by the webhook's secret, of
`<unix time>.<body>`: a receiver checks it, and that the time is recent, to know the request is dlboard's and not a replay.
"""

from __future__ import annotations

import hashlib
import hmac
import queue
import re
import secrets
import socket
import ssl
import threading
import time
import uuid
from http import HTTPStatus
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Self, override
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
from pydantic_settings import BaseSettings, SettingsConfigDict
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._version import __version__
from dlboard.serve import CommaSeparated

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dlboard.serve._custodian import Backoff

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
    def words(self) -> EventWords:
        """How the event is put in words: on the Webhooks tab, and in a post's `text`."""
        match self:
            case WebhookEvent.FINISHED:
                return EventWords("Finished", "finished")
            case WebhookEvent.FAILED:
                return EventWords("Failed", "failed")
            case WebhookEvent.CRASHED:
                return EventWords("Crashed", "crashed (its process stopped being heard from)")
            case WebhookEvent.ENDED:
                return EventWords("Ended without saying how", "ended without saying how")


class EventWords(NamedTuple):
    label: str
    """What the Webhooks tab calls the event."""
    verb: str
    """What a run did, in the sentence a post's `text` is."""


class WebhookSettings(BaseSettings):
    """`DLBOARD_WEBHOOK_*`: the server's own say over webhooks."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_WEBHOOK_")

    allowed_hosts: CommaSeparated[frozenset[str]] = frozenset()
    """Comma-separated hosts a webhook may reach though they resolve to a non-public address (an internal chat server)."""
    public_url: HttpUrl | None = None
    """Where people reach the server (scheme and host), for the links a post has; unset, where its owner reached it."""
    max_per_project: PositiveInt = 10
    timeout_s: PositiveFloat = 5.0
    """How long a delivery waits to connect, and then for the answer."""
    attempts: PositiveInt = 10
    """Tries a delivery gets, spaced by the custodian's `Backoff`, before its webhook is turned off."""
    interval_s: PositiveInt = 30
    """How often the server looks for runs' ends to announce, and for deliveries to try again."""
    expire_h: PositiveFloat = 48
    """
    How long after a run ends its end may still be posted. One that hasn't been by then (the server was down, or the
    plugin was taken off the deployment's list and is listed again) is dropped, never posted that late.
    """

    def expired(self, done_at: AwareDatetime) -> bool:
        """Whether a run's end, recorded at `done_at`, is too old to post."""
        return pendulum.instance(done_at) + pendulum.duration(hours=self.expire_h) < pendulum.now(
            pendulum.UTC
        )

    def check_expiry(self, backoff: Backoff) -> None:
        """Refuse an expiry a delivery's tries outlast: a webhook that keeps failing must be turned off, not go quiet."""
        tried_for = backoff.total(self.attempts)
        if tried_for >= pendulum.duration(hours=self.expire_h):
            msg = (
                f"DLBOARD_WEBHOOK_EXPIRE_H ({self.expire_h:g}) must be longer than DLBOARD_WEBHOOK_ATTEMPTS "
                f"({self.attempts}) tries take with the custodian's retry backoff ({tried_for.in_words()})"
            )
            raise ValueError(msg)

    @field_validator("allowed_hosts", mode="after")
    @classmethod
    def _lower(cls, hosts: frozenset[str]) -> frozenset[str]:
        """As a URL's host is compared: in lower case."""
        return frozenset(host.lower() for host in hosts)


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


class Named(BaseModel, frozen=True, extra="forbid"):
    """A project or experiment, as a post names it."""

    id: int
    name: str | None


class RunSummary(BaseModel, frozen=True, extra="forbid"):
    """The run, as it was when it ended."""

    id: int
    name: str | None
    status: models.RunStatus | None
    created_at: AwareDatetime
    ended_at: AwareDatetime | None
    """When the run ended, by its client's word (or when a lost run was last heard from)."""
    done_at: AwareDatetime
    """When the server recorded the end: unique to one end of the run."""
    duration_s: float | None


class WebhookPayload(BaseModel, frozen=True, extra="forbid"):
    """
    What a webhook is sent: the event, the project, experiment and run, and a link to the run.

    `text` says it in a sentence, which is all a Slack incoming webhook reads (it ignores the rest). Made once, when the
    run's end is found, so every try of a delivery posts the same body, describing the run as it ended.
    """

    event: WebhookEvent
    text: str
    project: Named
    experiment: Named
    run: RunSummary
    url: HttpUrl

    @classmethod
    def of(  # noqa: PLR0913 -- the event, what it is of, when, and where its link points
        cls,
        event: WebhookEvent,
        run: models.Run,
        done_at: AwareDatetime,
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
            text=f"{run.name or f'Run {run.id}'} {event.words.verb}{after}, in {where}: {link}",
            project=Named(id=project.id, name=project.name),
            experiment=Named(id=experiment.id, name=experiment.name),
            run=RunSummary(
                id=run.id,
                name=run.name,
                status=run.status,
                created_at=run.created_at,
                ended_at=run.ended_at,
                done_at=done_at,
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


def _answered(hook: Webhook, address: IPv4Address | IPv6Address, body: bytes, headers: dict[str, str]) -> int:
    """Post `body` to `hook` at `address`, and the status it was answered with: the rest of the answer is never read."""
    target = parse_url(str(hook.url))
    pinned = target._replace(host=f"[{address}]" if address.version == 6 else str(address))  # noqa: PLR2004 -- IPv6
    timeout = WebhookSettings().timeout_s
    with requests.Session() as session:
        session.trust_env = False  # a proxy would connect to the host by its name, undoing the address check
        session.mount(f"{target.scheme}://", _PinnedAdapter(urlsplit(str(hook.url)).hostname or ""))
        with session.post(
            pinned.url,
            data=body,
            headers={"Host": target.netloc or "", **headers},
            timeout=(timeout, timeout),
            allow_redirects=False,
            stream=True,  # the status is all a delivery needs: a body, however big or slow, is left unread
        ) as response:
            return response.status_code


def send(hook: Webhook, payload: WebhookPayload, delivery_id: uuid.UUID) -> None:
    """
    Post `payload` to `hook`, signed, at an address `_address` allows; `RefusedError` saying why if it didn't land.

    A post takes `DLBOARD_WEBHOOK_TIMEOUT_S` twice at the most, to connect and then to be answered, however the
    receiver trickles its answer (a timeout bounds each read, not their sum): one not answered by then is tried again
    later, and the thread still waiting on it is let go of, so it holds up no other delivery.
    """
    address = _address(hook.url)
    host = parse_url(str(hook.url)).host
    body = payload.model_dump_json().encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"dlboard/{__version__}",
        EVENT_HEADER: payload.event,
        DELIVERY_HEADER: str(delivery_id),
        SIGNATURE_HEADER: Signature.of(hook.secret, body, int(time.time())).header,
    }
    answers: queue.SimpleQueue[int | Exception] = queue.SimpleQueue()

    def post() -> None:
        try:
            answers.put(_answered(hook, address, body, headers))
        except Exception as error:  # noqa: BLE001 -- said where the post was asked for, below
            answers.put(error)

    threading.Thread(target=post, name="dlboard-webhook-post", daemon=True).start()
    deadline_s = 2 * WebhookSettings().timeout_s
    try:
        answer = answers.get(timeout=deadline_s)
    except queue.Empty:
        msg = f"{host} didn't answer within {deadline_s:g}s"
        raise RefusedError(msg) from None
    match answer:
        case requests.RequestException():
            msg = f"{host} didn't answer ({type(answer).__name__})"
            raise RefusedError(msg) from answer
        case Exception():
            raise answer
        case int() if not HTTPStatus.OK <= answer < HTTPStatus.MULTIPLE_CHOICES:
            msg = f"{host} answered {answer}"
            raise RefusedError(msg, Failure.of(answer))
        case int():
            return


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
