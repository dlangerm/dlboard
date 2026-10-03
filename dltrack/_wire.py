"""
The REST wire contract: path shapes and request/response bodies, shared by client and server.

Neither `dltrack.client` nor `dltrack.plugins.backend.basic_rest_backend` (the server side) is a
parent package of the other, so this lives at the top level rather than inside either -- importing
a submodule always runs its parent package's `__init__.py` first, and the server importing
anything nested under `dltrack.client` would mean importing the server needs whatever
`dltrack/client/__init__.py` eagerly imports (`DLTrackLogger`, and so `torch`), for a module that
never actually touches either. Pure `pydantic`/`typing` -- no heavy dependency on either side.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel

from dltrack import models

METADATA_PART_SUFFIX: Final = ".json"
"""Appended to an artifact file's multipart part name to name the part carrying its `NewArtifact` JSON."""

WHOAMI_PATH: Final = "whoami"

DLTRACK_USER_HEADER: Final = "X-Dltrack-User"
"""Carries the client's best-effort identity (see `dltrack._identity.resolve_username`).

Never trusted blindly -- a missing/blank header falls back to the same best-effort resolution the
client itself falls back to. This is attribution, not authentication: nothing here proves a caller
actually is who the header claims. Read server-side by `dltrack.plugins.auth.anonymous`, the one
provider that doesn't verify identity another way.
"""


def create_path(
    model: type[BaseModel] | None,
    base_url: str = "/",
    api_version: Literal[1] = 1,
) -> str:
    """Make a consistent API path."""
    match api_version:
        case 1:
            return f"{base_url}/create/{model.__name__ if model is not None else ''}".strip("/")


def entity_path(model: type[BaseModel], entity_id: str = "<int:entity_id>", base_url: str = "/") -> str:
    """Make a consistent API path for an action on a single existing entity, e.g. `project/<id>`."""
    return f"{base_url}/{model.__name__.lower()}/{entity_id}".strip("/")


def get_or_create_path(model: type[BaseModel], base_url: str = "/") -> str:
    """Make a consistent API path for the get-or-create-by-name idiom, e.g. `project/get-or-create`."""
    return f"{base_url}/{model.__name__.lower()}/get-or-create".strip("/")


class Identity(BaseModel, frozen=True, extra="forbid"):
    """Who the server authenticated a request as -- what `whoami` returns."""

    id: int
    username: str


class GetOrCreateProject(BaseModel, frozen=True, extra="forbid"):
    """Request body: find a project by name, creating it (with `description`) if it's missing."""

    name: str
    description: str = ""


class GetOrCreateExperiment(BaseModel, frozen=True, extra="forbid"):
    """Request body: find an experiment by name within a project, creating it if it's missing."""

    project_id: int
    name: str = "default"
    source: models.ExperimentSource | None = None
