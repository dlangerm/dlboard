"""Routes for doing general api things."""

from time import perf_counter

import dash
from flask import Response, request
from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.plugins.utilities import get_artifact_store, get_data_store

_log = get_logger(__name__)


@dash.hooks.route("/log-batch", methods=["POST"])
def func() -> dict[str, str]:
    try:
        store = get_data_store()
        store.log_metrics(models.LoggedMetrics.model_validate(metric) for metric in request.json)
    except Exception:
        _log.exception("Error logging metrics")
        raise
    return {}


@dash.hooks.route("/log-hyperparams", methods=["POST"])
def log_hyperparams() -> dict[str, str]:
    try:
        store = get_data_store()
        store.log_hyperparams(models.NewHyperParams.model_validate(request.json))
    except Exception:
        _log.exception("Error logging hyperparameters")
        raise
    return {}


@dash.hooks.route(f"/create/{models.Experiment.__name__}", methods=["POST"])
def create_experiment() -> dict[str, str]:
    try:
        store = get_data_store()
        exp = store.create_experiment(models.NewExperiment.model_validate(request.json))
        return exp.model_dump(mode="json")
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(f"/create/{models.Run.__name__}", methods=["POST"])
def create_run() -> dict[str, str]:
    try:
        _log.info("create new run")
        store = get_data_store()
        run = store.create_run(models.NewRun.model_validate(request.json))
        return run.model_dump(mode="json")
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(f"/create/{models.Project.__name__}", methods=["POST"])
def create_project() -> dict[str, str]:
    try:
        store = get_data_store()
        project = store.create_project(models.NewProject.model_validate(request.json))
        return project.model_dump(mode="json")
    except Exception:
        _log.exception("Error creating project")
        raise


@dash.hooks.route(f"create/{models.Artifact.__name__}", methods=["POST"])
def log_artifact() -> dict[str, str]:
    try:
        _log.info("log artifact batch")
        store = get_artifact_store()
        t0 = perf_counter()
        jsons = (
            models.NewArtifact.model_validate_json(f.stream.read().decode())
            for f in request.files.values()
            if f.content_type == "application/json"
        )
        try:
            store.log_artifacts(
                jsons,
                request.files,
            )
        finally:
            _log.info("logging artifacts took %.3f seconds", perf_counter() - t0)
    except Exception:
        _log.exception("failed to create new artifacts")
        raise
    return {}


@dash.hooks.route("artifact/<string:artifact_url>", methods=["GET"])  # pyright: ignore[reportArgumentType]
def download_artifact(artifact_url: str) -> Response:
    try:
        return get_artifact_store().download_artifact(
            AnyUrl(artifact_url.replace("=", "/").replace("+", ":"))
        )
    except Exception:
        _log.exception("failed to load artifact")
        raise
