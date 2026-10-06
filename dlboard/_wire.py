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

from typing import Final

from pydantic import BaseModel

from dlboard import models
from dlboard._compat import StrEnum

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


def create_path(resource: Resource, base_url: str = "/") -> str:
    """The REST path to create a new `resource`, e.g. `api/v1/projects`."""
    return f"{base_url}/{API_PREFIX}/{resource}".strip("/")


def entity_path(resource: Resource, entity_id: str = "<int:entity_id>", base_url: str = "/") -> str:
    """The REST path for an action on one existing `resource`, e.g. `api/v1/projects/<id>`."""
    return f"{base_url}/{API_PREFIX}/{resource}/{entity_id}".strip("/")


def get_or_create_path(resource: Resource, base_url: str = "/") -> str:
    """The REST path for the get-or-create-by-name idiom, e.g. `api/v1/projects/get-or-create`."""
    return f"{base_url}/{API_PREFIX}/{resource}/get-or-create".strip("/")


class Identity(BaseModel, frozen=True, extra="ignore"):
    """Who the server authenticated a request as, plus enough to let a client self-diagnose -- what `whoami` returns."""

    id: int
    username: str
    server_version: str
    """The running server's `dlboard-server` version -- e.g. for an error message, never parsed by the client."""
    api_version: int = API_VERSION
    """The REST API major this server speaks. A client that doesn't recognize it should refuse to
    proceed rather than guess -- see `DLBoardLogger.__init__`."""


class GetOrCreateProject(BaseModel, frozen=True, extra="ignore"):
    """Request body: find a project by name, creating it (with `description`) if it's missing."""

    name: str
    description: str = ""


class GetOrCreateExperiment(BaseModel, frozen=True, extra="ignore"):
    """Request body: find an experiment by name within a project, creating it if it's missing."""

    project_id: int
    name: str = "default"
    source: models.ExperimentSource | None = None
