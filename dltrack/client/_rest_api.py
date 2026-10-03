"""
The client's HTTP transport: `BasicDltrackAPI`, built on the wire contract in `dltrack._wire`.

Pure `requests` -- no `dash`/`flask`, so importing this (and anything that imports it, like
`dltrack.client.dltrack_logger`) never needs the server stack installed.
"""

from __future__ import annotations

import itertools
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import requests
from pydantic import AnyUrl, BaseModel, SecretStr
from pydantic_settings import BaseSettings
from structlog.stdlib import get_logger

from dltrack import models
from dltrack._identity import resolve_username
from dltrack._wire import (
    DLTRACK_USER_HEADER,
    METADATA_PART_SUFFIX,
    WHOAMI_PATH,
    GetOrCreateExperiment,
    GetOrCreateProject,
    Identity,
    create_path,
    entity_path,
    get_or_create_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_log = get_logger(__name__)

DEFAULT_SERVER_URL: Final = "http://localhost:8050"
"""Matches `dltrack serve local`'s own default host/port (see `ServerRuntimeOptions` in `_cli.py`)."""


class ClientAuthSettings(BaseSettings):
    """The client's credentials, from env vars."""

    dltrack_api_key: SecretStr | None = None
    """An API token (`dlt_...`) from the server's Account page. Not needed by a server that doesn't verify identity."""


class AuthenticationFailedError(RuntimeError):
    """The server didn't accept this client's credentials (or it sent none, and the server needs some)."""


def _post_request[R: BaseModel](
    path: str, body: BaseModel, return_model: type[R], headers: dict[str, str] | None = None
) -> R:
    try:
        res = requests.post(path, json=body.model_dump(mode="json"), headers=headers)
        res.raise_for_status()
        return return_model.model_validate(res.json())
    except Exception:
        _log.exception("Failed to post %s", body)
        raise


def _create_request[R: BaseModel](
    create_model: BaseModel,
    return_model: type[R],
    base_url: str = "/",
    api_version: Literal[1] = 1,
    headers: dict[str, str] | None = None,
) -> R:
    return _post_request(
        create_path(return_model, base_url, api_version), create_model, return_model, headers
    )


def _get_or_create_request[R: BaseModel](
    body: BaseModel,
    return_model: type[R],
    base_url: str = "/",
    headers: dict[str, str] | None = None,
) -> R:
    return _post_request(get_or_create_path(return_model, base_url), body, return_model, headers)


class BasicDltrackAPI:
    """API class."""

    def __init__(self, base_url: str = DEFAULT_SERVER_URL, api_key: SecretStr | None = None) -> None:
        """Initialize the API class, authenticating with `api_key` (default: the `DLTRACK_API_KEY` env var)."""
        self.base_url = base_url
        api_key = api_key or ClientAuthSettings().dltrack_api_key
        # Resolved once per process (not per call): who's actually running this is not going to
        # change mid-run, and every request this client makes should be attributed consistently.
        # The username header only matters to a server that doesn't verify identity; one that does
        # goes by the API key alone.
        self._headers = {DLTRACK_USER_HEADER: resolve_username()}
        if api_key is not None:
            self._headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"

    def whoami(self) -> Identity:
        """Who the server authenticates this client as. Raises `AuthenticationFailedError` if nobody."""
        res = requests.get(f"{self.base_url}/{WHOAMI_PATH}", headers=self._headers)
        if res.status_code == HTTPStatus.UNAUTHORIZED:
            msg = (
                f"The dltrack server at {self.base_url} rejected this client's credentials. Create an API "
                "token from your Account page and set it as DLTRACK_API_KEY (or pass `api_key`)."
            )
            raise AuthenticationFailedError(msg)
        res.raise_for_status()
        return Identity.model_validate(res.json())

    def create_project(self, new_project: models.NewProject) -> models.Project:
        """Create a new project."""
        return _create_request(new_project, models.Project, self.base_url, headers=self._headers)

    def get_or_create_project(self, name: str, description: str = "") -> models.Project:
        """Get the project named `name`, creating it (with `description`) if it doesn't exist yet."""
        return _get_or_create_request(
            GetOrCreateProject(name=name, description=description),
            models.Project,
            self.base_url,
            headers=self._headers,
        )

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        return _create_request(new_experiment, models.Experiment, self.base_url, headers=self._headers)

    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment:
        """Get the named experiment within `project_id`, creating it if it doesn't exist yet."""
        return _get_or_create_request(
            GetOrCreateExperiment(project_id=project_id, name=name, source=source),
            models.Experiment,
            self.base_url,
            headers=self._headers,
        )

    def create_run(self, run: models.NewRun) -> models.Run:
        """Initialize a new run."""
        return _create_request(run, models.Run, self.base_url, headers=self._headers)

    def get_run(self, run_id: int) -> models.Run:
        """The existing run `run_id`. Raises `requests.HTTPError` (404) if it's missing, deleted, or not yours to see."""
        res = requests.get(f"{self.base_url}/{entity_path(models.Run, str(run_id))}", headers=self._headers)
        res.raise_for_status()
        return models.Run.model_validate(res.json())

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters."""
        return _create_request(hyperparams, models.HyperParams, self.base_url, headers=self._headers)

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Log a batch of metrics."""
        res = requests.post(
            create_path(models.LoggedMetrics, self.base_url),
            json=[m.model_dump(mode="json") for m in metrics],
            headers=self._headers,
        )
        res.raise_for_status()

    def log_artifact_batch(self, artifacts: Iterable[tuple[models.NewArtifact, Path | AnyUrl]]) -> None:
        """
        Log a batch of artifacts in one request, split into an upload batch and a link batch.

        Each artifact is paired with either a local file to upload (written by its own
        `to_artifact`, e.g. `client.artifacts.image.Image`) or an already-stored ref to link
        (`client.artifacts.link.Link`) -- see `AnyArtifact.to_artifact`.
        """
        uploads: list[tuple[models.NewArtifact, Path]] = []
        links: list[tuple[models.NewArtifact, AnyUrl]] = []
        for artifact, source in artifacts:
            match source:
                case Path():
                    uploads.append((artifact, source))
                case AnyUrl():
                    links.append((artifact, source))
        if uploads:
            self._upload_artifacts(uploads)
        if links:
            self._link_artifacts(links)

    def _upload_artifacts(self, uploads: list[tuple[models.NewArtifact, Path]]) -> None:
        """
        Upload a batch of artifacts in one request.

        Every artifact travels as two multipart parts named by its position in the batch -- its
        file (`0`) and its metadata (`0.json`) -- never by its key: many artifacts routinely share
        a key (one image per step), and the server matches metadata to file by part name.
        """
        # `requests` never closes the file handles it's handed -- opened explicitly (not inline
        # in the `files=` generator below) so they can be closed in `finally` regardless of
        # whether the request succeeds, rather than leaking a descriptor per artifact.
        opened = [path.open("rb") for _, path in uploads]
        try:
            res = requests.post(
                create_path(models.Artifact, self.base_url),
                files=itertools.chain(
                    *(
                        (
                            (str(i), (a.fname, fh, "application/octet")),
                            (
                                f"{i}{METADATA_PART_SUFFIX}",
                                (a.fname, a.model_dump_json(), "application/json"),
                            ),
                        )
                        for i, ((a, _path), fh) in enumerate(zip(uploads, opened, strict=True))
                    )
                ),
                headers=self._headers,
            )
            res.raise_for_status()
        except Exception:
            _log.exception("Failed to upload %d artifact(s)", len(uploads))
            raise
        finally:
            for fh in opened:
                fh.close()

    def _link_artifacts(self, links: list[tuple[models.NewArtifact, AnyUrl]]) -> None:
        """Register a batch of already-stored artifacts by ref, with no bytes uploaded."""
        body = [
            models.NewArtifactLink.model_validate(a.model_dump() | {"ref": ref}).model_dump(mode="json")
            for a, ref in links
        ]
        try:
            res = requests.post(
                create_path(models.NewArtifactLink, self.base_url), json=body, headers=self._headers
            )
            res.raise_for_status()
        except Exception:
            _log.exception("Failed to link %d artifact(s)", len(links))
            raise
