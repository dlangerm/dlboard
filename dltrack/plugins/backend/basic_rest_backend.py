"""Routes for doing general api things."""

import itertools
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final, Iterable, Literal

import dash
import requests
from flask import request
from pydantic import AnyUrl, BaseModel, ValidationError
from structlog.stdlib import get_logger
from werkzeug.datastructures import FileStorage

from dltrack import models
from dltrack._identity import resolve_username
from dltrack.models import DataStore
from dltrack.plugins.auth.anonymous import DLTRACK_USER_HEADER
from dltrack.serve import get_artifact_store, get_current_user, get_data_store

_log = get_logger(__name__)

DEFAULT_SERVER_URL: Final = "http://localhost:8050"
"""Matches `dltrack serve local`'s own default host/port (see `ServerRuntimeOptions` in `_cli.py`)."""

_METADATA_PART_SUFFIX: Final = ".json"
"""Appended to an artifact file's multipart part name to name the part carrying its `NewArtifact` JSON."""


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


class GetOrCreateProject(BaseModel, frozen=True, extra="forbid"):
    """Request body: find a project by name, creating it (with `description`) if it's missing."""

    name: str
    description: str = ""


class GetOrCreateExperiment(BaseModel, frozen=True, extra="forbid"):
    """Request body: find an experiment by name within a project, creating it if it's missing."""

    project_id: int
    name: str = "default"
    source: models.ExperimentSource | None = None


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

    def __init__(self, base_url: str = DEFAULT_SERVER_URL) -> None:
        """Initialize the API class."""
        self.base_url = base_url
        # Resolved once per process (not per call): who's actually running this is not going to
        # change mid-run, and every request this client makes should be attributed consistently.
        self._headers = {DLTRACK_USER_HEADER: resolve_username()}

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
        `to_artifact`, e.g. `plugins.artifacts.image.Image`) or an already-stored ref to link
        (`plugins.artifacts.link.Link`) -- see `AnyArtifact.to_artifact`.
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
                                f"{i}{_METADATA_PART_SUFFIX}",
                                (a.fname, a.model_dump_json(), "application/json"),
                            ),
                        )
                        for i, ((a, _path), fh) in enumerate(zip(uploads, opened, strict=True))
                    )
                ),
                headers=self._headers,
            )
            res.raise_for_status()
        finally:
            for fh in opened:
                fh.close()

    def _link_artifacts(self, links: list[tuple[models.NewArtifact, AnyUrl]]) -> None:
        """Register a batch of already-stored artifacts by ref, with no bytes uploaded."""
        body = [
            models.NewArtifactLink.model_validate(a.model_dump() | {"ref": ref}).model_dump(mode="json")
            for a, ref in links
        ]
        res = requests.post(
            create_path(models.NewArtifactLink, self.base_url), json=body, headers=self._headers
        )
        res.raise_for_status()


# -- Route handlers -------------------------------------------------------------------------------
#
# Split out from the `@dash.hooks.route`-decorated view functions below so the actual request
# handling -- stamping `created_by`, calling the store -- is testable directly against a real
# store, without needing a live Flask request context. Each takes an already-resolved `actor`
# rather than resolving identity itself, so identity resolution (see `dltrack.serve.get_current_user`)
# stays entirely the auth provider's concern.


def handle_create_project(store: DataStore[...], body: dict[str, Any], actor: models.User) -> dict[str, Any]:
    """Create a project, attributed to `actor`."""
    project = models.NewProject.model_validate(body).model_copy(update={"created_by": actor.id})
    return store.create_project(project).model_dump(mode="json")


def handle_create_experiment(
    store: DataStore[...], body: dict[str, Any], actor: models.User
) -> dict[str, Any]:
    """Create an experiment, attributed to `actor`."""
    experiment = models.NewExperiment.model_validate(body).model_copy(update={"created_by": actor.id})
    return store.create_experiment(experiment).model_dump(mode="json")


def handle_get_or_create_project(
    store: DataStore[...], body: dict[str, Any], actor: models.User
) -> dict[str, Any]:
    """Get or create a project by name, attributed to `actor` if it's newly created."""
    req = GetOrCreateProject.model_validate(body)
    return store.get_or_create_project(req.name, req.description, created_by=actor.id).model_dump(mode="json")


def handle_get_or_create_experiment(
    store: DataStore[...], body: dict[str, Any], actor: models.User
) -> dict[str, Any]:
    """Get or create an experiment by name within a project, attributed to `actor` if newly created."""
    req = GetOrCreateExperiment.model_validate(body)
    return store.get_or_create_experiment(
        req.project_id, req.name, created_by=actor.id, source=req.source
    ).model_dump(mode="json")


def handle_create_run(store: DataStore[...], body: dict[str, Any], actor: models.User) -> dict[str, Any]:
    """Create a run, attributed to `actor`."""
    run = models.NewRun.model_validate(body).model_copy(update={"created_by": actor.id})
    return store.create_run(run).model_dump(mode="json")


def handle_log_artifacts(
    store: models.ArtifactStore[...],
    artifacts: Iterable[tuple[models.NewArtifact, FileStorage]],
    actor: models.User,
) -> None:
    """Log a batch of uploaded artifacts, each attributed to `actor`."""
    store.log_artifacts((a.model_copy(update={"created_by": actor.id}), file) for a, file in artifacts)


def handle_link_artifacts(
    data_store: DataStore[...],
    artifact_store: models.ArtifactStore[...],
    links: Iterable[models.NewArtifactLink],
    actor: models.User,
) -> None:
    """
    Register a batch of already-stored artifacts, each attributed to `actor`.

    Unlike an upload (recorded asynchronously once its blob is actually written -- see
    `BlobArtifactStore.ingest_stored_artifacts`), a link moves no bytes, so it's recorded
    synchronously here: `link_artifacts` raises `UnservableArtifactRefError` for the whole batch before
    any of it reaches `data_store`, rather than a client having to wait and find out later.
    """
    pairs = [
        (
            models.NewArtifact.model_validate(link.model_dump(exclude={"ref"}) | {"created_by": actor.id}),
            link.ref,
        )
        for link in links
    ]
    data_store.log_artifact_refs(artifact_store.link_artifacts(pairs))


def handle_delete_project(store: DataStore[...], project_id: int, actor: models.User) -> None:
    """Soft-delete a project, attributed to `actor`. Raises `PermissionError` without `Scope.PROJECT_DELETE`."""
    store.delete_project(project_id, actor)


def handle_restore_project(store: DataStore[...], project_id: int, actor: models.User) -> None:
    """Restore a soft-deleted project, attributed to `actor`. Raises `PermissionError` without `Scope.RESTORE`."""
    store.restore_project(project_id, actor)


def handle_delete_experiment(store: DataStore[...], experiment_id: int, actor: models.User) -> None:
    """Soft-delete an experiment, attributed to `actor`. Raises `PermissionError` without `Scope.EXPERIMENT_DELETE`."""
    store.delete_experiment(experiment_id, actor)


def handle_restore_experiment(store: DataStore[...], experiment_id: int, actor: models.User) -> None:
    """Restore a soft-deleted experiment, attributed to `actor`. Raises `PermissionError` without `Scope.RESTORE`."""
    store.restore_experiment(experiment_id, actor)


def handle_delete_run(store: DataStore[...], run_id: int, actor: models.User) -> None:
    """Soft-delete a run, attributed to `actor`. Raises `PermissionError` without `Scope.RUN_DELETE`."""
    store.delete_run(run_id, actor)


def handle_restore_run(store: DataStore[...], run_id: int, actor: models.User) -> None:
    """Restore a soft-deleted run, attributed to `actor`. Raises `PermissionError` without `Scope.RESTORE`."""
    store.restore_run(run_id, actor)


def handle_delete_artifact(store: DataStore[...], artifact_id: int, actor: models.User) -> None:
    """Soft-delete a single artifact, attributed to `actor`. Raises `PermissionError` without `Scope.ARTIFACT_DELETE`."""
    store.delete_artifact(artifact_id, actor)


def handle_restore_artifact(store: DataStore[...], artifact_id: int, actor: models.User) -> None:
    """Restore a soft-deleted artifact, attributed to `actor`. Raises `PermissionError` without `Scope.RESTORE`."""
    store.restore_artifact(artifact_id, actor)


# -- Routes -----------------------------------------------------------------------------------
#
# Plain functions, not `@dash.hooks.route`-decorated -- that decorator registers into a
# process-wide global registry the moment this module is *imported*, regardless of whether it's
# ever plugged into an app. That would mean anything importing this module for another reason
# (e.g. the client importing `BasicDltrackAPI`) risks registering these routes globally, and a
# process building more than one `Dash` app would register them twice. `_ROUTES` + `plug()` below
# register them onto one specific `app.server` instead, exactly once, only when this plugin is
# actually used.


def log_batch() -> dict[str, str]:
    """Log a batch of metrics."""
    try:
        store = get_data_store()
        store.log_metrics(models.LoggedMetrics.model_validate(metric) for metric in request.json)
    except Exception:
        _log.exception("Error logging metrics")
        raise
    return {}


def log_hyperparams() -> dict[str, str]:
    """Log the hyperparameters for an experiment."""
    try:
        store = get_data_store()
        return store.log_hyperparams(models.NewHyperParams.model_validate(request.json)).model_dump(
            mode="json"
        )
    except Exception:
        _log.exception("Error logging hyperparameters")
        raise


def create_experiment() -> dict[str, Any]:
    """Create a new experiment for a project."""
    try:
        store = get_data_store()
        return handle_create_experiment(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating")
        raise


def create_run() -> dict[str, Any]:
    """Create a new run for an experiment."""
    try:
        _log.info("create new run")
        store = get_data_store()
        return handle_create_run(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating")
        raise


def create_project() -> dict[str, Any]:
    """Create a new project."""
    try:
        store = get_data_store()
        return handle_create_project(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating project")
        raise


def get_or_create_project() -> dict[str, Any]:
    """Get or create a project by name."""
    try:
        store = get_data_store()
        return handle_get_or_create_project(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error getting or creating project")
        raise


def get_or_create_experiment() -> dict[str, Any]:
    """Get or create an experiment by name within a project."""
    try:
        store = get_data_store()
        return handle_get_or_create_experiment(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error getting or creating experiment")
        raise


def log_artifact() -> dict[str, str]:
    """Log an artifact with metadata and files."""
    try:
        _log.debug("log artifact batch")
        t0 = perf_counter()
        pairs = (
            (
                models.NewArtifact.model_validate_json(part.stream.read().decode()),
                request.files[name.removesuffix(_METADATA_PART_SUFFIX)],
            )
            for name, part in request.files.items()
            if part.content_type == "application/json"
        )
        try:
            handle_log_artifacts(get_artifact_store(), pairs, get_current_user(get_data_store()))
        finally:
            _log.debug("logging artifacts took %.3f seconds", perf_counter() - t0)
    except Exception:
        _log.exception("failed to create new artifacts")
        raise
    return {}


def link_artifact() -> dict[str, str]:
    """Register a batch of already-stored artifacts by ref, with no bytes uploaded."""
    try:
        links = [models.NewArtifactLink.model_validate(item) for item in request.json]
        handle_link_artifacts(
            get_data_store(), get_artifact_store(), links, get_current_user(get_data_store())
        )
    except Exception:
        _log.exception("failed to link new artifacts")
        raise
    return {}


def delete_project(entity_id: int) -> dict[str, str]:
    """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
    try:
        store = get_data_store()
        handle_delete_project(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting project %s", entity_id)
        raise
    return {}


def restore_project(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted project and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_project(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring project %s", entity_id)
        raise
    return {}


def delete_experiment(entity_id: int) -> dict[str, str]:
    """Soft-delete an experiment and cascade to its runs and artifacts."""
    try:
        store = get_data_store()
        handle_delete_experiment(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting experiment %s", entity_id)
        raise
    return {}


def restore_experiment(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted experiment and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_experiment(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring experiment %s", entity_id)
        raise
    return {}


def delete_run(entity_id: int) -> dict[str, str]:
    """Soft-delete a run and cascade to its artifacts."""
    try:
        store = get_data_store()
        handle_delete_run(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting run %s", entity_id)
        raise
    return {}


def restore_run(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted run and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_run(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring run %s", entity_id)
        raise
    return {}


def delete_artifact(entity_id: int) -> dict[str, str]:
    """Soft-delete a single artifact."""
    try:
        store = get_data_store()
        handle_delete_artifact(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting artifact %s", entity_id)
        raise
    return {}


def restore_artifact(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted artifact."""
    try:
        store = get_data_store()
        handle_restore_artifact(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring artifact %s", entity_id)
        raise
    return {}


_ROUTES: tuple[tuple[str, list[str], Callable[..., Any]], ...] = (
    (create_path(models.LoggedMetrics), ["POST"], log_batch),
    (create_path(models.HyperParams), ["POST"], log_hyperparams),
    (create_path(models.Experiment), ["POST"], create_experiment),
    (create_path(models.Run), ["POST"], create_run),
    (create_path(models.Project), ["POST"], create_project),
    (get_or_create_path(models.Project), ["POST"], get_or_create_project),
    (get_or_create_path(models.Experiment), ["POST"], get_or_create_experiment),
    (create_path(models.Artifact), ["POST"], log_artifact),
    (create_path(models.NewArtifactLink), ["POST"], link_artifact),
    (entity_path(models.Project), ["DELETE"], delete_project),
    (f"{entity_path(models.Project)}/restore", ["POST"], restore_project),
    (entity_path(models.Experiment), ["DELETE"], delete_experiment),
    (f"{entity_path(models.Experiment)}/restore", ["POST"], restore_experiment),
    (entity_path(models.Run), ["DELETE"], delete_run),
    (f"{entity_path(models.Run)}/restore", ["POST"], restore_run),
    (entity_path(models.Artifact), ["DELETE"], delete_artifact),
    (f"{entity_path(models.Artifact)}/restore", ["POST"], restore_artifact),
)


def _handle_permission_error(err: PermissionError) -> tuple[dict[str, str], int]:
    """Map a missing-scope `PermissionError` to a 403, instead of Flask's default 500."""
    return {"error": str(err)}, 403


def _handle_validation_error(err: ValidationError) -> tuple[dict[str, str], int]:
    """
    Map an invalid request body to a 400, instead of Flask's default 500.

    The distinction matters to the client's shipping processes (`dltrack_logger.ship_batches`): a
    4xx means "this batch can never succeed, drop it", a 5xx means "transient, retry it".
    """
    return {"error": str(err)}, 400


def _handle_unservable_ref_error(err: models.UnservableArtifactRefError) -> tuple[dict[str, str], int]:
    """Map a link to a ref this server's `ArtifactStore` won't serve to a 400, not a 500 -- same 4xx-vs-5xx reasoning as `_handle_validation_error`."""
    return {"error": str(err)}, 400


def plug(app: dash.Dash) -> None:
    """Register this module's REST routes and its error -> HTTP status mappings onto `app`."""
    for path, methods, view_func in _ROUTES:
        # `_ROUTES` paths are prefix-relative (no leading slash, see `create_path`/`entity_path`)
        # -- mirrors how Dash's own `@dash.hooks.route`-registered routes get mounted, so this
        # still works under a non-default `routes_pathname_prefix`.
        prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType,reportUnknownMemberType]
        full_path = prefix + path
        app.server.add_url_rule(full_path, endpoint=full_path, view_func=view_func, methods=methods)
    app.server.errorhandler(PermissionError)(_handle_permission_error)
    app.server.errorhandler(ValidationError)(_handle_validation_error)
    app.server.errorhandler(models.UnservableArtifactRefError)(_handle_unservable_ref_error)
