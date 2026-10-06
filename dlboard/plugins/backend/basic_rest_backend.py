"""Routes for doing general api things."""

from time import perf_counter
from typing import Any, Callable, Iterable

import dash
from flask import request
from pydantic import ValidationError
from structlog.stdlib import get_logger
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import NotFound

from dlboard import models
from dlboard._version import __version__
from dlboard._wire import (
    METADATA_PART_SUFFIX,
    WHOAMI_PATH,
    GetOrCreateExperiment,
    GetOrCreateProject,
    Identity,
    Resource,
    create_path,
    entity_path,
    get_or_create_path,
)
from dlboard.models import DataStore
from dlboard.serve import get_artifact_store, get_current_user, get_data_store

_log = get_logger(__name__)


# -- Route handlers -------------------------------------------------------------------------------
#
# Split out from the `@dash.hooks.route`-decorated view functions below so the actual request
# handling -- stamping `created_by`, calling the store -- is testable directly against a real
# store, without needing a live Flask request context. Each takes an already-resolved `actor`
# rather than resolving identity itself, so identity resolution (see `dlboard.serve.get_current_user`)
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


def handle_get_run(store: DataStore[...], run_id: int) -> dict[str, Any]:
    """An existing run, for a client attaching to it. 404 when it's missing, deleted or not visible to the caller."""
    run = store.get_run(run_id)
    if run is None:
        raise NotFound
    return run.model_dump(mode="json")


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
# (e.g. the client importing `BasicDlboardAPI`) risks registering these routes globally, and a
# process building more than one `Dash` app would register them twice. `_ROUTES` + `plug()` below
# register them onto one specific `app.server` instead, exactly once, only when this plugin is
# actually used.


def whoami() -> dict[str, Any]:
    """Who this request was authenticated as, plus this server's version -- the client's handshake."""
    user = get_current_user()
    return Identity(id=user.id, username=user.username, server_version=__version__).model_dump(mode="json")


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
        return handle_create_experiment(store, request.json, get_current_user())
    except Exception:
        _log.exception("Error creating")
        raise


def create_run() -> dict[str, Any]:
    """Create a new run for an experiment."""
    try:
        _log.info("create new run")
        store = get_data_store()
        return handle_create_run(store, request.json, get_current_user())
    except Exception:
        _log.exception("Error creating")
        raise


def get_run(entity_id: int) -> dict[str, Any]:
    """Look up an existing run by id."""
    return handle_get_run(get_data_store(), entity_id)


def create_project() -> dict[str, Any]:
    """Create a new project."""
    try:
        store = get_data_store()
        return handle_create_project(store, request.json, get_current_user())
    except Exception:
        _log.exception("Error creating project")
        raise


def get_or_create_project() -> dict[str, Any]:
    """Get or create a project by name."""
    try:
        store = get_data_store()
        return handle_get_or_create_project(store, request.json, get_current_user())
    except Exception:
        _log.exception("Error getting or creating project")
        raise


def get_or_create_experiment() -> dict[str, Any]:
    """Get or create an experiment by name within a project."""
    try:
        store = get_data_store()
        return handle_get_or_create_experiment(store, request.json, get_current_user())
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
                request.files[name.removesuffix(METADATA_PART_SUFFIX)],
            )
            for name, part in request.files.items()
            if part.content_type == "application/json"
        )
        try:
            handle_log_artifacts(get_artifact_store(), pairs, get_current_user())
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
        handle_link_artifacts(get_data_store(), get_artifact_store(), links, get_current_user())
    except Exception:
        _log.exception("failed to link new artifacts")
        raise
    return {}


def delete_project(entity_id: int) -> dict[str, str]:
    """Soft-delete a project and cascade to its experiments, runs, and artifacts."""
    try:
        store = get_data_store()
        handle_delete_project(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error deleting project %s", entity_id)
        raise
    return {}


def restore_project(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted project and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_project(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error restoring project %s", entity_id)
        raise
    return {}


def delete_experiment(entity_id: int) -> dict[str, str]:
    """Soft-delete an experiment and cascade to its runs and artifacts."""
    try:
        store = get_data_store()
        handle_delete_experiment(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error deleting experiment %s", entity_id)
        raise
    return {}


def restore_experiment(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted experiment and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_experiment(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error restoring experiment %s", entity_id)
        raise
    return {}


def delete_run(entity_id: int) -> dict[str, str]:
    """Soft-delete a run and cascade to its artifacts."""
    try:
        store = get_data_store()
        handle_delete_run(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error deleting run %s", entity_id)
        raise
    return {}


def restore_run(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted run and everything deleted with it."""
    try:
        store = get_data_store()
        handle_restore_run(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error restoring run %s", entity_id)
        raise
    return {}


def delete_artifact(entity_id: int) -> dict[str, str]:
    """Soft-delete a single artifact."""
    try:
        store = get_data_store()
        handle_delete_artifact(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error deleting artifact %s", entity_id)
        raise
    return {}


def restore_artifact(entity_id: int) -> dict[str, str]:
    """Restore a soft-deleted artifact."""
    try:
        store = get_data_store()
        handle_restore_artifact(store, entity_id, get_current_user())
    except Exception:
        _log.exception("Error restoring artifact %s", entity_id)
        raise
    return {}


_ROUTES: tuple[tuple[str, list[str], Callable[..., Any]], ...] = (
    (WHOAMI_PATH, ["GET"], whoami),
    (create_path(Resource.METRICS), ["POST"], log_batch),
    (create_path(Resource.HYPERPARAMS), ["POST"], log_hyperparams),
    (create_path(Resource.EXPERIMENTS), ["POST"], create_experiment),
    (create_path(Resource.RUNS), ["POST"], create_run),
    (create_path(Resource.PROJECTS), ["POST"], create_project),
    (get_or_create_path(Resource.PROJECTS), ["POST"], get_or_create_project),
    (get_or_create_path(Resource.EXPERIMENTS), ["POST"], get_or_create_experiment),
    (create_path(Resource.ARTIFACTS), ["POST"], log_artifact),
    (create_path(Resource.ARTIFACT_LINKS), ["POST"], link_artifact),
    (entity_path(Resource.PROJECTS), ["DELETE"], delete_project),
    (f"{entity_path(Resource.PROJECTS)}/restore", ["POST"], restore_project),
    (entity_path(Resource.EXPERIMENTS), ["DELETE"], delete_experiment),
    (f"{entity_path(Resource.EXPERIMENTS)}/restore", ["POST"], restore_experiment),
    (entity_path(Resource.RUNS), ["GET"], get_run),
    (entity_path(Resource.RUNS), ["DELETE"], delete_run),
    (f"{entity_path(Resource.RUNS)}/restore", ["POST"], restore_run),
    (entity_path(Resource.ARTIFACTS), ["DELETE"], delete_artifact),
    (f"{entity_path(Resource.ARTIFACTS)}/restore", ["POST"], restore_artifact),
)


def _handle_permission_error(err: PermissionError) -> tuple[dict[str, str], int]:
    """Map a missing-scope `PermissionError` to a 403, instead of Flask's default 500."""
    return {"error": str(err)}, 403


def _handle_validation_error(err: ValidationError) -> tuple[dict[str, str], int]:
    """
    Map an invalid request body to a 400, instead of Flask's default 500.

    The distinction matters to the client's shipping processes (`dlboard_logger.ship_batches`): a
    4xx means "this batch can never succeed, drop it", a 5xx means "transient, retry it".
    """
    return {"error": str(err)}, 400


def _handle_ambiguous_project(err: models.AmbiguousProjectError) -> tuple[dict[str, str], int]:
    """Map a project name the caller can write to more than one of to a 409 -- they must pick one by id."""
    return {"error": str(err)}, 409


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
        # One path can carry several methods (`GET`/`DELETE` on a run), so the endpoint name has to
        # be per-handler, not per-path, or Flask rejects the second as a duplicate endpoint.
        app.server.add_url_rule(
            full_path, endpoint=f"{full_path}:{view_func.__name__}", view_func=view_func, methods=methods
        )
    app.server.errorhandler(PermissionError)(_handle_permission_error)
    app.server.errorhandler(ValidationError)(_handle_validation_error)
    app.server.errorhandler(models.UnservableArtifactRefError)(_handle_unservable_ref_error)
    app.server.errorhandler(models.AmbiguousProjectError)(_handle_ambiguous_project)
