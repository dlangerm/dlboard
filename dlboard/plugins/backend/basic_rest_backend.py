"""Routes for doing general api things."""

from dataclasses import dataclass
from http import HTTPStatus
from time import perf_counter
from typing import Any, Callable, Final, Iterable

import dash
from flask import request
from pydantic import ValidationError
from structlog.stdlib import get_logger
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import NotFound

from dlboard import models
from dlboard._telemetry import Attributes, attribute
from dlboard._version import __version__
from dlboard._wire import (
    METADATA_PART_SUFFIX,
    WHOAMI_PATH,
    FinishRun,
    GetOrCreateExperiment,
    GetOrCreateProject,
    Identity,
    MarkRun,
    Resource,
    RunCall,
    create_path,
    entity_path,
    get_or_create_path,
    run_path,
)
from dlboard.models import DataStore
from dlboard.serve import get_artifact_store, get_current_user, get_data_store
from dlboard.serve.telemetry import AttributeKey, Counter, NoAttributes, Tally, Unit

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _FinishedAttributes(Attributes):
    status: models.RunStatus = attribute(AttributeKey.RUN_STATUS)


_METRIC_VALUES = Counter[NoAttributes](
    "dlboard.ingest.metric_values", Unit.VALUES, "Metric values accepted from training runs."
)
_ARTIFACTS = Counter[NoAttributes]("dlboard.ingest.artifacts", Unit.ARTIFACTS, "Artifacts uploaded.")
_ARTIFACT_BYTES = Counter[NoAttributes](
    "dlboard.ingest.artifact.size", Unit.BYTES, "Bytes of artifact uploads."
)
_RUNS_CREATED = Counter[NoAttributes]("dlboard.runs.created", Unit.RUNS, "Runs started.")
_RUNS_FINISHED = Counter[_FinishedAttributes](
    "dlboard.runs.finished", Unit.RUNS, "Runs reported done, by how they ended."
)


def _values_in(logged: models.LoggedMetrics) -> int:
    return len(logged.metrics)


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
    created = store.create_run(run)
    _RUNS_CREATED.add(1)
    return created.model_dump(mode="json")


def handle_finish_run(store: DataStore[...], run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Record that run `run_id`'s client is done, as and when it reports (`FinishRun`). 404 as `handle_get_run`."""
    req = FinishRun.model_validate(body)
    if store.get_run(run_id) is None:
        raise NotFound
    finished = store.finish_run(run_id, req.status, req.ended_at)
    _RUNS_FINISHED.add(1, _FinishedAttributes(status=req.status))
    return finished.model_dump(mode="json")


def handle_mark_run(
    store: DataStore[...], run_id: int, body: dict[str, Any], actor: models.User
) -> dict[str, Any]:
    """
    Keep and/or pin run `run_id` as `actor` (`MarkRun`), and say how the run stands. 404 as `handle_get_run`.

    The keep comes first: it needs more than a pin does, so a writer who may pin but not keep marks nothing.
    """
    req = MarkRun.model_validate(body)
    if store.get_run(run_id) is None:
        raise NotFound
    if req.keep:
        store.keep_runs(frozenset({run_id}), actor)
    if req.pin is not None:
        store.pin_runs(actor.id, frozenset({run_id}), req.pin)
    return handle_get_run(store, run_id)


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
    batch = Tally((models.LoggedMetrics.model_validate(metric) for metric in request.json), weigh=_values_in)
    get_data_store().log_metrics(batch)
    _METRIC_VALUES.add(batch.total)
    return {}


def log_hyperparams() -> dict[str, str]:
    """Log the hyperparameters for an experiment."""
    hyperparams = models.NewHyperParams.model_validate(request.json)
    return get_data_store().log_hyperparams(hyperparams).model_dump(mode="json")


def record_run_context() -> dict[str, Any]:
    """Record what produced a run: its commit, command, packages and machine. A run keeps the first one."""
    context = models.NewRunContext.model_validate(request.json)
    return get_data_store().record_run_context(context).model_dump(mode="json")


def create_experiment() -> dict[str, Any]:
    """Create a new experiment for a project."""
    return handle_create_experiment(get_data_store(), request.json, get_current_user())


def create_run() -> dict[str, Any]:
    """Create a new run for an experiment."""
    return handle_create_run(get_data_store(), request.json, get_current_user())


def get_run(entity_id: int) -> dict[str, Any]:
    """Look up an existing run by id."""
    return handle_get_run(get_data_store(), entity_id)


def finish_run(entity_id: int) -> dict[str, Any]:
    """A run's client reports it is done (finished or failed)."""
    return handle_finish_run(get_data_store(), entity_id, request.json)


def mark_run(entity_id: int) -> dict[str, Any]:
    """A run's client pins and/or keeps it, as it is launched."""
    return handle_mark_run(get_data_store(), entity_id, request.json, get_current_user())


def heartbeat(entity_id: int) -> dict[str, Any]:
    """A run's client says it is alive, between batches. 404 for a run that is gone, as `handle_get_run`."""
    store = get_data_store()
    if store.get_run(entity_id) is None:
        raise NotFound
    store.heartbeat(entity_id)
    return {}


def create_project() -> dict[str, Any]:
    """Create a new project."""
    return handle_create_project(get_data_store(), request.json, get_current_user())


def get_or_create_project() -> dict[str, Any]:
    """Get or create a project by name."""
    return handle_get_or_create_project(get_data_store(), request.json, get_current_user())


def get_or_create_experiment() -> dict[str, Any]:
    """Get or create an experiment by name within a project."""
    return handle_get_or_create_experiment(get_data_store(), request.json, get_current_user())


def log_artifact() -> dict[str, str]:
    """Log an artifact with metadata and files."""
    t0 = perf_counter()
    pairs = Tally(
        (
            models.NewArtifact.model_validate_json(part.stream.read().decode()),
            request.files[name.removesuffix(METADATA_PART_SUFFIX)],
        )
        for name, part in request.files.items()
        if part.content_type == "application/json"
    )
    try:
        handle_log_artifacts(get_artifact_store(), pairs, get_current_user())
        _ARTIFACTS.add(pairs.total)
        _ARTIFACT_BYTES.add(request.content_length or 0)
    finally:
        _log.debug("logging artifacts took %.3f seconds", perf_counter() - t0)
    return {}


def link_artifact() -> dict[str, str]:
    """Register a batch of already-stored artifacts by ref, with no bytes uploaded."""
    links = [models.NewArtifactLink.model_validate(item) for item in request.json]
    handle_link_artifacts(get_data_store(), get_artifact_store(), links, get_current_user())
    return {}


def _entity_route(
    change: Callable[[DataStore[...], int, models.User], None],
) -> Callable[[int], dict[str, str]]:
    """The route that makes `change` (a delete or a restore) to the entity its path names, as the request's user."""

    def route(entity_id: int) -> dict[str, str]:
        change(get_data_store(), entity_id, get_current_user())
        return {}

    return route


_ROUTES: tuple[tuple[str, list[str], Callable[..., Any]], ...] = (
    (WHOAMI_PATH, ["GET"], whoami),
    (create_path(Resource.METRICS), ["POST"], log_batch),
    (create_path(Resource.HYPERPARAMS), ["POST"], log_hyperparams),
    (create_path(Resource.RUN_CONTEXTS), ["POST"], record_run_context),
    (create_path(Resource.EXPERIMENTS), ["POST"], create_experiment),
    (create_path(Resource.RUNS), ["POST"], create_run),
    (create_path(Resource.PROJECTS), ["POST"], create_project),
    (get_or_create_path(Resource.PROJECTS), ["POST"], get_or_create_project),
    (get_or_create_path(Resource.EXPERIMENTS), ["POST"], get_or_create_experiment),
    (create_path(Resource.ARTIFACTS), ["POST"], log_artifact),
    (create_path(Resource.ARTIFACT_LINKS), ["POST"], link_artifact),
    (entity_path(Resource.PROJECTS), ["DELETE"], _entity_route(handle_delete_project)),
    (f"{entity_path(Resource.PROJECTS)}/restore", ["POST"], _entity_route(handle_restore_project)),
    (entity_path(Resource.EXPERIMENTS), ["DELETE"], _entity_route(handle_delete_experiment)),
    (f"{entity_path(Resource.EXPERIMENTS)}/restore", ["POST"], _entity_route(handle_restore_experiment)),
    (entity_path(Resource.RUNS), ["GET"], get_run),
    (entity_path(Resource.RUNS), ["DELETE"], _entity_route(handle_delete_run)),
    (run_path(RunCall.FINISH), ["POST"], finish_run),
    (run_path(RunCall.MARK), ["POST"], mark_run),
    (run_path(RunCall.HEARTBEAT), ["POST"], heartbeat),
    (f"{entity_path(Resource.RUNS)}/restore", ["POST"], _entity_route(handle_restore_run)),
    (entity_path(Resource.ARTIFACTS), ["DELETE"], _entity_route(handle_delete_artifact)),
    (f"{entity_path(Resource.ARTIFACTS)}/restore", ["POST"], _entity_route(handle_restore_artifact)),
)

_ERROR_STATUSES: Final[tuple[tuple[type[Exception], HTTPStatus], ...]] = (
    (PermissionError, HTTPStatus.FORBIDDEN),
    (ValidationError, HTTPStatus.BAD_REQUEST),
    # A link to a ref this server's `ArtifactStore` won't serve.
    (models.UnservableArtifactRefError, HTTPStatus.BAD_REQUEST),
    # A run (or what holds it) deleted, or never there: what was logged to it can never land.
    (models.EntityGoneError, HTTPStatus.NOT_FOUND),
    # A project name the caller can write to more than one of: they must pick one by id.
    (models.AmbiguousProjectError, HTTPStatus.CONFLICT),
    # A plugin refuses new runs in the project for now, and says why.
    (models.ProjectLockedError, HTTPStatus.LOCKED),
    # An artifact store that can't take an upload right now.
    (models.ArtifactStoreUnavailableError, HTTPStatus.SERVICE_UNAVAILABLE),
)
"""
The status each of the errors a request can be refused with is answered with, instead of Flask's default 500.

The client's shipping processes go by the difference (`dlboard.client.run.classify_failure`): a 4xx is never sent again,
a 5xx is retried. So an error that a retry can't get past must be a 4xx here, or a client retries it forever.
"""


def _error_response(err: Exception) -> tuple[dict[str, str], HTTPStatus, dict[str, str]]:
    """`err` answered with its status in `_ERROR_STATUSES`, and its message for the client to show."""
    status = next(status for kind, status in _ERROR_STATUSES if isinstance(err, kind))
    headers: dict[str, str] = {"Retry-After": "5"} if status is HTTPStatus.SERVICE_UNAVAILABLE else {}
    return {"error": str(err)}, status, headers


def plug(app: dash.Dash) -> None:
    """Register this module's REST routes and its error -> HTTP status mappings onto `app`."""
    for path, methods, view_func in _ROUTES:
        # `_ROUTES` paths are prefix-relative (no leading slash, see `create_path`/`entity_path`)
        # -- mirrors how Dash's own `@dash.hooks.route`-registered routes get mounted, so this
        # still works under a non-default `routes_pathname_prefix`.
        prefix = str(app.config.routes_pathname_prefix)  # pyrefly: ignore [unknown-argument-type]
        full_path = prefix + path
        # One path can carry several methods (`GET`/`DELETE` on a run), so the endpoint name has to
        # be per method, not per path, or Flask rejects the second as a duplicate endpoint.
        app.server.add_url_rule(
            full_path, endpoint=f"{' '.join(methods)} {full_path}", view_func=view_func, methods=methods
        )
    for kind, _status in _ERROR_STATUSES:
        app.server.errorhandler(kind)(_error_response)
