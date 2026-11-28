"""
The REST wire contract: path shapes and request/response bodies, shared by client and server.

Neither `dlboard.client` nor `dlboard.plugins.backend.basic_rest_backend` (the server side) is a
parent package of the other, so this lives at the top level rather than inside either -- importing
a submodule always runs its parent package's `__init__.py` first, and the server importing
anything nested under `dlboard.client` would mean importing the server needs whatever
`dlboard/client/__init__.py` eagerly imports (`DLBoardLogger`, and so `torch`), for a module that
never actually touches either. Pure `pydantic`/`typing` -- no heavy dependency on either side.
"""

from __future__ import annotations

from typing import Final, Generic, TypeVar

import pendulum
from pydantic import AwareDatetime, BaseModel, Field, RootModel, TypeAdapter, field_validator

from dlboard import models
from dlboard._compat import StrEnum
from dlboard._content_encoding import ContentEncoding

_Item = TypeVar("_Item")
"""Pre-3.12 `TypeVar` style (the client's floor is 3.10): what a `Page` holds."""

API_VERSION: Final = 1
"""
The REST API major this version of dlboard speaks (see `Identity.api_version`). Bumped only for a
breaking change to an existing path's request/response shape or status-code semantics -- an
additive change (a new optional field, a new path) doesn't need one, since `extra="ignore"` on
every model here is what makes those safe to skip across.
"""

API_PREFIX: Final = f"api/v{API_VERSION}"
"""Every REST path lives under this, so a path never again depends on a Python class's `__name__`
the way it used to -- see `Resource`."""

METADATA_PART_SUFFIX: Final = ".json"
"""Appended to an artifact file's multipart part name to name the part carrying its `NewArtifact` JSON."""

WHOAMI_PATH: Final = f"{API_PREFIX}/whoami"

DLBOARD_USER_HEADER: Final = "X-Dlboard-User"
"""Carries the client's best-effort identity (see `dlboard._identity.resolve_username`).

Never trusted blindly -- a missing/blank header falls back to the same best-effort resolution the
client itself falls back to. This is attribution, not authentication: nothing here proves a caller
actually is who the header claims. Read server-side by `dlboard.plugins.auth.anonymous`, the one
provider that doesn't verify identity another way.
"""

REQUEST_ID_HEADER: Final = "X-Request-Id"
"""
Names one request in the server's logs. A caller may supply it; the server otherwise makes one up. Every response
carries it, so a client that gets an error back can say which request failed, and the server's log line for it can be
found -- see `dlboard.client._rest_api`.
"""

DLBOARD_CLIENT_VERSION_HEADER: Final = "X-Dlboard-Client-Version"
"""The installed `dlboard` client distribution's version -- attribution only, like `DLBOARD_USER_HEADER`;
the server doesn't gate on it. See `Identity.server_version`/`api_version` for the direction a
client actually acts on."""


class Resource(StrEnum):
    """
    Every REST resource's path segment under `API_PREFIX`.

    The plural noun shared by a resource's "create" path (`.../<resource>`) and its "one existing
    entity" path (`.../<resource>/<id>`). Spelled out as literal strings, not derived from a
    model's `__name__`: a path changing because a model got renamed in a refactor would be a
    breaking change nobody intended.
    """

    PROJECTS = "projects"
    EXPERIMENTS = "experiments"
    RUNS = "runs"
    ARTIFACTS = "artifacts"
    ARTIFACT_LINKS = "artifact-links"
    METRICS = "metrics"
    HYPERPARAMS = "hyperparams"
    RUN_CONTEXTS = "run-contexts"


def create_path(resource: Resource, base_url: str = "/") -> str:
    """The REST path to create a new `resource`, e.g. `api/v1/projects`."""
    return f"{base_url}/{API_PREFIX}/{resource}".strip("/")


def entity_path(resource: Resource, entity_id: str = "<int:entity_id>", base_url: str = "/") -> str:
    """The REST path for an action on one existing `resource`, e.g. `api/v1/projects/<id>`."""
    return f"{base_url}/{API_PREFIX}/{resource}/{entity_id}".strip("/")


def get_or_create_path(resource: Resource, base_url: str = "/") -> str:
    """The REST path for the get-or-create-by-name idiom, e.g. `api/v1/projects/get-or-create`."""
    return f"{base_url}/{API_PREFIX}/{resource}/get-or-create".strip("/")


class RunCall(StrEnum):
    """What a run's client says to the run it created: the last segment of `api/v1/runs/<id>/<call>`."""

    FINISH = "finish"
    """It is done, and how."""
    MARK = "mark"
    """It is to be pinned and/or kept (`MarkRun`)."""
    HEARTBEAT = "heartbeat"
    """It is still alive, between batches (a long evaluation): so it is not given up as lost."""
    TOKEN = "token"
    """A run token for it, for a job to log to it with (`RunToken`): asked with a full token or a session, never a run token."""


def run_path(call: RunCall, run_id: str = "<int:entity_id>", base_url: str = "/") -> str:
    """The REST path of a call on one run, e.g. `api/v1/runs/<id>/finish`."""
    return f"{entity_path(Resource.RUNS, run_id, base_url)}/{call}"


class Read(StrEnum):
    """
    What is read back of one entity, a page at a time: the last segment of `api/v1/<resource>/<id>/<read>`.

    Every read answers one `Page`, of at most the server's own cap for it however many are asked for, and the cursor of the
    page after it. A page is sought from that cursor, never counted to: the thousandth costs what the first does.
    """

    EXPERIMENTS = "experiments"
    """A project's experiments, newest first."""
    RUNS = "runs"
    """An experiment's runs, newest first: `search` by name, `tag` (repeated) by tag."""
    METRIC_KEYS = "metric-keys"
    """The keys an experiment's runs logged metrics under, in order."""
    ARTIFACT_KEYS = "artifact-keys"
    """The keys an experiment's runs logged artifacts under, in order."""
    HYPERPARAMS = "hyperparams"
    """A run's hyperparameters: one object, not a page."""
    SUMMARY = "summary"
    """A run's latest value under each key, a page of keys at a time (`LatestValue`)."""
    SERIES = "series"
    """Every value a run logged under `key`, by step (`MetricPoint`)."""
    ARTIFACTS = "artifacts"
    """A run's artifacts under `key`: those logged at a step by step, then those logged at none (`ArtifactInfo`)."""


def read_path(resource: Resource, read: Read, entity_id: str = "<int:entity_id>", base_url: str = "/") -> str:
    """The REST path of a read of one entity, e.g. `api/v1/runs/<id>/series`."""
    return f"{entity_path(resource, entity_id, base_url)}/{read}"


ARTIFACT_CONTENT_PATH: Final = "artifact/{artifact_id}"
"""Where an artifact's bytes are served, by its id (`ArtifactInfo.id`): the route the UI's charts fetch them from too."""


class Page(BaseModel, Generic[_Item], frozen=True, extra="ignore"):
    """One page of a `Read`: its items, and the cursor that asks for the page after it, `None` on the last."""

    items: list[_Item]
    next_cursor: str | None = None
    """Opaque: handed back as `cursor` as it is. Only the server reads what is in it."""


class MetricPoint(BaseModel, frozen=True, extra="ignore"):
    """A value of a run's series: what it logged under a key at a step, and when, by its clock."""

    step: int
    value: float | None
    timestamp_utc: AwareDatetime


class RunHyperparams(RootModel[models.FlatHparamDict]):
    """A run's hyperparameters as it logged them (`Read.HYPERPARAMS`): one flat object, empty if it logged none."""


class LatestValue(BaseModel, frozen=True, extra="ignore"):
    """A key a run logged under, and the last value it logged there."""

    key: str
    value: float | None


class ArtifactInfo(BaseModel, frozen=True, extra="ignore"):
    """An artifact a run logged, without its bytes (`ARTIFACT_CONTENT_PATH`) or where the server keeps them."""

    id: int
    key: str
    fname: str
    step: int | None
    run_id: int
    created_at: AwareDatetime
    tags: dict[str, models.TagValue] = {}


_NAMES: Final = TypeAdapter(tuple[str, ...])


class Identity(BaseModel, frozen=True, extra="ignore"):
    """Who the server authenticated a request as, plus enough to let a client self-diagnose -- what `whoami` returns."""

    id: int
    username: str
    server_version: str
    """The running server's `dlboard` version -- e.g. for an error message, never parsed by the client."""
    api_version: int = API_VERSION
    """The REST API major this server speaks. A client that doesn't recognize it should refuse to
    proceed rather than guess -- see `DLBoardLogger.__init__`."""
    run_id: int | None = None
    """The one run the request's run token logs to, or `None` for any other credential (and from an older server)."""
    accepted_encodings: tuple[ContentEncoding, ...] = ()
    """The `Content-Encoding`s this server takes a request body in that this client knows; none from an older server."""

    @field_validator("accepted_encodings", mode="before")
    @classmethod
    def _known(
        cls,
        value: object,  # dlboard-ignore[forbid-string-ignore] -- the raw input
    ) -> tuple[str, ...]:
        """A newer server may list an encoding this client doesn't know, which it then just doesn't use."""
        return tuple(name for name in _NAMES.validate_python(value) if name in set(ContentEncoding))


class RunToken(BaseModel, frozen=True, extra="ignore"):
    """A run token just minted: its only copy (the server keeps a hash), the run it logs to, and when it stops working."""

    token: str
    run_id: int
    expires_at: AwareDatetime


class GetOrCreateProject(BaseModel, frozen=True, extra="ignore"):
    """Request body: find a project by name, creating it (with `description`) if it's missing."""

    name: str
    description: str = ""


class FinishRun(BaseModel, frozen=True, extra="ignore"):
    """
    Request body: a run's client reports it is done, how, and when.

    `ended_at` is the client's clock, like the run's own `created_at`, so the duration between them
    never mixes two machines' clocks.
    """

    status: models.RunStatus
    ended_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))

    @field_validator("status")
    @classmethod
    def _must_be_an_ending(cls, status: models.RunStatus) -> models.RunStatus:
        """`running` is only ever what a run starts as: reporting it done makes no sense."""
        if status is models.RunStatus.RUNNING:
            msg = "a run is finished as finished, failed or unknown, not running"
            raise ValueError(msg)
        return status


class MarkRun(BaseModel, frozen=True, extra="ignore"):
    """
    Request body: a run's client pins it (for the caller), keeps it and/or tags it, as it is launched.

    Pinning is the caller's own, in a colour, as the UI does it. Keeping takes the run out of retention's reach, which an
    editor of its project may do, as is tagging it. Asking for none does nothing.
    """

    pin: models.PinColor | None = None
    keep: bool = False
    tags: list[models.TagName] = Field(default_factory=list[str], max_length=100)
    """Tags to give the run, as a project editor does (`DataStore.tag_runs`); an older server ignores them."""


class GetOrCreateExperiment(BaseModel, frozen=True, extra="ignore"):
    """Request body: find an experiment by name within a project, creating it if it's missing."""

    project_id: int
    name: str = "default"
    source: models.ExperimentSource | None = None
