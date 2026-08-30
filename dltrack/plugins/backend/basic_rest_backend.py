"""Routes for doing general api things."""

import itertools
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Final, Iterable, Literal

import dash
import requests
from flask import Response, request
from pydantic import AnyUrl, BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack._identity import resolve_username
from dltrack.models import DataStore
from dltrack.plugins.auth.anonymous import DLTRACK_USER_HEADER
from dltrack.serve import get_artifact_store, get_current_user, get_data_store

_log = get_logger(__name__)

DEFAULT_SERVER_URL: Final = "http://localhost:8050"
"""Matches `dltrack serve local`'s own default host/port (see `ServerRuntimeOptions` in `_cli.py`)."""


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

    def log_artifact_batch(self, artifacts: Iterable[models.NewArtifact], files: list[Path]) -> None:
        """Log a batch of artifacts."""
        keys = {a.key for a in artifacts}
        for k in keys:
            common_artifacts = ((a, files[i]) for i, a in enumerate(artifacts) if a.key == k)

            res = requests.post(
                create_path(models.Artifact, self.base_url),
                files=itertools.chain(
                    *(
                        (
                            (k, (a.fname, f.open("rb"), "application/octet")),
                            (k + ".json", (a.fname, a.model_dump_json(), "application/json")),
                        )
                        for a, f in common_artifacts
                    )
                ),
                headers=self._headers,
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
    artifacts: Iterable[models.NewArtifact],
    files: Any,  # noqa: ANN401 -- `werkzeug.datastructures.FileStorage` mapping, matches `ArtifactStore.log_artifacts`
    actor: models.User,
) -> None:
    """Log a batch of artifacts, each attributed to `actor`."""
    stamped = (a.model_copy(update={"created_by": actor.id}) for a in artifacts)
    store.log_artifacts(stamped, files)


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
        _log.info("log artifact batch")
        t0 = perf_counter()
        jsons = (
            models.NewArtifact.model_validate_json(f.stream.read().decode())
            for f in request.files.values()
            if f.content_type == "application/json"
        )
        try:
            handle_log_artifacts(
                get_artifact_store(),
                jsons,
                request.files,
                get_current_user(get_data_store()),
            )
        finally:
            _log.info("logging artifacts took %.3f seconds", perf_counter() - t0)
    except Exception:
        _log.exception("failed to create new artifacts")
        raise
    return {}


def download_artifact(artifact_url: str) -> Response:
    """Download an artifact at a specified url."""
    try:
        return get_artifact_store().download_artifact(
            AnyUrl(artifact_url.replace("=", "/").replace("+", ":"))
        )
    except Exception:
        _log.exception("failed to load artifact")
        raise


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
    ("artifact/<string:artifact_url>", ["GET"], download_artifact),
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


def plug(app: dash.Dash) -> None:
    """Register this module's REST routes and the `PermissionError` -> 403 mapping onto `app`."""
    for path, methods, view_func in _ROUTES:
        # `_ROUTES` paths are prefix-relative (no leading slash, see `create_path`/`entity_path`)
        # -- mirrors how Dash's own `@dash.hooks.route`-registered routes get mounted, so this
        # still works under a non-default `routes_pathname_prefix`.
        prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType,reportUnknownMemberType]
        full_path = prefix + path
        app.server.add_url_rule(full_path, endpoint=full_path, view_func=view_func, methods=methods)
    app.server.errorhandler(PermissionError)(_handle_permission_error)
