"""Routes for doing general api things."""

import itertools
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Literal

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


def _create_request[R: BaseModel](
    create_model: BaseModel,
    return_model: type[R],
    base_url: str = "/",
    api_version: Literal[1] = 1,
    headers: dict[str, str] | None = None,
) -> R:
    try:
        url = create_path(return_model, base_url, api_version)
        res = requests.post(url, json=create_model.model_dump(mode="json"), headers=headers)
        res.raise_for_status()
        return return_model.model_validate(res.json())
    except Exception:
        _log.exception("Failed to crete %s", create_model)
        raise


class BasicDltrackAPI:
    """API class."""

    def __init__(self, base_url: str = "http://localhost:8050") -> None:
        """Initialize the API class."""
        self.base_url = base_url
        # Resolved once per process (not per call): who's actually running this is not going to
        # change mid-run, and every request this client makes should be attributed consistently.
        self._headers = {DLTRACK_USER_HEADER: resolve_username()}

    def create_project(self, new_project: models.NewProject) -> models.Project:
        """Create a new project."""
        return _create_request(new_project, models.Project, self.base_url, headers=self._headers)

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        return _create_request(new_experiment, models.Experiment, self.base_url, headers=self._headers)

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
    """Soft-delete a project, attributed to `actor`."""
    store.delete_project(project_id, actor_id=actor.id)


def handle_restore_project(store: DataStore[...], project_id: int, actor: models.User) -> None:
    """Restore a soft-deleted project, attributed to `actor`."""
    store.restore_project(project_id, actor_id=actor.id)


def handle_delete_experiment(store: DataStore[...], experiment_id: int, actor: models.User) -> None:
    """Soft-delete an experiment, attributed to `actor`."""
    store.delete_experiment(experiment_id, actor_id=actor.id)


def handle_restore_experiment(store: DataStore[...], experiment_id: int, actor: models.User) -> None:
    """Restore a soft-deleted experiment, attributed to `actor`."""
    store.restore_experiment(experiment_id, actor_id=actor.id)


def handle_delete_run(store: DataStore[...], run_id: int, actor: models.User) -> None:
    """Soft-delete a run, attributed to `actor`."""
    store.delete_run(run_id, actor_id=actor.id)


def handle_restore_run(store: DataStore[...], run_id: int, actor: models.User) -> None:
    """Restore a soft-deleted run, attributed to `actor`."""
    store.restore_run(run_id, actor_id=actor.id)


def handle_delete_artifact(store: DataStore[...], artifact_id: int, actor: models.User) -> None:
    """Soft-delete a single artifact, attributed to `actor`."""
    store.delete_artifact(artifact_id, actor_id=actor.id)


def handle_restore_artifact(store: DataStore[...], artifact_id: int, actor: models.User) -> None:
    """Restore a soft-deleted artifact, attributed to `actor`."""
    store.restore_artifact(artifact_id, actor_id=actor.id)


# -- Routes -----------------------------------------------------------------------------------


@dash.hooks.route(create_path(models.LoggedMetrics), methods=["POST"])
def log_batch() -> dict[str, str]:
    """Log a batch of metrics."""
    try:
        store = get_data_store()
        store.log_metrics(models.LoggedMetrics.model_validate(metric) for metric in request.json)
    except Exception:
        _log.exception("Error logging metrics")
        raise
    return {}


@dash.hooks.route(create_path(models.HyperParams), methods=["POST"])
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
    return {}


@dash.hooks.route(create_path(models.Experiment), methods=["POST"])
def create_experiment() -> dict[str, Any]:
    """Create a new experiment for a project."""
    try:
        store = get_data_store()
        return handle_create_experiment(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(create_path(models.Run), methods=["POST"])
def create_run() -> dict[str, Any]:
    """Create a new run for an experiment."""
    try:
        _log.info("create new run")
        store = get_data_store()
        return handle_create_run(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(create_path(models.Project), methods=["POST"])
def create_project() -> dict[str, Any]:
    """Create a new project."""
    try:
        store = get_data_store()
        return handle_create_project(store, request.json, get_current_user(store))
    except Exception:
        _log.exception("Error creating project")
        raise


@dash.hooks.route(create_path(models.Artifact), methods=["POST"])
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


@dash.hooks.route("artifact/<string:artifact_url>", methods=["GET"])  # pyright: ignore[reportArgumentType]
def download_artifact(artifact_url: str) -> Response:
    """Download an artifact at a specified url."""
    try:
        return get_artifact_store().download_artifact(
            AnyUrl(artifact_url.replace("=", "/").replace("+", ":"))
        )
    except Exception:
        _log.exception("failed to load artifact")
        raise


@dash.hooks.route(entity_path(models.Project), methods=["DELETE"])  # pyright: ignore[reportArgumentType]
def delete_project(entity_id: int) -> dict[str, str]:
    """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
    try:
        store = get_data_store()
        handle_delete_project(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting project %s", entity_id)
        raise
    return {}


@dash.hooks.route(f"{entity_path(models.Project)}/restore", methods=["POST"])  # pyright: ignore[reportArgumentType]
def restore_project(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted project and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_project(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring project %s", entity_id)
        raise
    return {}


@dash.hooks.route(entity_path(models.Experiment), methods=["DELETE"])  # pyright: ignore[reportArgumentType]
def delete_experiment(entity_id: int) -> dict[str, str]:
    """Soft-delete an experiment and cascade to its runs and artifacts."""
    try:
        store = get_data_store()
        handle_delete_experiment(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting experiment %s", entity_id)
        raise
    return {}


@dash.hooks.route(f"{entity_path(models.Experiment)}/restore", methods=["POST"])  # pyright: ignore[reportArgumentType]
def restore_experiment(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted experiment and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_experiment(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring experiment %s", entity_id)
        raise
    return {}


@dash.hooks.route(entity_path(models.Run), methods=["DELETE"])  # pyright: ignore[reportArgumentType]
def delete_run(entity_id: int) -> dict[str, str]:
    """Soft-delete a run and cascade to its artifacts."""
    try:
        store = get_data_store()
        handle_delete_run(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting run %s", entity_id)
        raise
    return {}


@dash.hooks.route(f"{entity_path(models.Run)}/restore", methods=["POST"])  # pyright: ignore[reportArgumentType]
def restore_run(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted run and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_run(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring run %s", entity_id)
        raise
    return {}


@dash.hooks.route(entity_path(models.Artifact), methods=["DELETE"])  # pyright: ignore[reportArgumentType]
def delete_artifact(entity_id: int) -> dict[str, str]:
    """Soft-delete a single artifact."""
    try:
        store = get_data_store()
        handle_delete_artifact(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error deleting artifact %s", entity_id)
        raise
    return {}


@dash.hooks.route(f"{entity_path(models.Artifact)}/restore", methods=["POST"])  # pyright: ignore[reportArgumentType]
def restore_artifact(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted artifact."""
    try:
        store = get_data_store()
        handle_restore_artifact(store, entity_id, get_current_user(store))
    except Exception:
        _log.exception("Error restoring artifact %s", entity_id)
        raise
    return {}
