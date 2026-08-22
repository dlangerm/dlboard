"""Routes for doing general api things."""

import itertools
from pathlib import Path
from time import perf_counter
from typing import Iterable, Literal

import dash
import requests
from flask import Response, request
from pydantic import AnyUrl, BaseModel
from structlog.stdlib import get_logger

from dltrack import models
from dltrack.serve import get_artifact_store, get_data_store

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


def _create_request[R: BaseModel](
    create_model: BaseModel,
    return_model: type[R],
    base_url: str = "/",
    api_version: Literal[1] = 1,
) -> R:
    try:
        url = create_path(return_model, base_url, api_version)
        res = requests.post(url, json=create_model.model_dump(mode="json"))
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

    def create_project(self, new_project: models.NewProject) -> models.Project:
        """Create a new project."""
        return _create_request(new_project, models.Project, self.base_url)

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        return _create_request(new_experiment, models.Experiment, self.base_url)

    def create_run(self, run: models.NewRun) -> models.Run:
        """Initialize a new run."""
        return _create_request(run, models.Run, self.base_url)

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters."""
        return _create_request(hyperparams, models.HyperParams, self.base_url)

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Log a batch of metrics."""
        res = requests.post(
            create_path(models.LoggedMetrics, self.base_url),
            json=[m.model_dump(mode="json") for m in metrics],
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
            )
            res.raise_for_status()


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
def create_experiment() -> dict[str, str]:
    """Create a new experiment for a project."""
    try:
        store = get_data_store()
        return store.create_experiment(models.NewExperiment.model_validate(request.json)).model_dump(
            mode="json"
        )
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(create_path(models.Run), methods=["POST"])
def create_run() -> dict[str, str]:
    """Create a new run for an experiment."""
    try:
        _log.info("create new run")
        store = get_data_store()
        return store.create_run(models.NewRun.model_validate(request.json)).model_dump(mode="json")
    except Exception:
        _log.exception("Error creating")
        raise


@dash.hooks.route(create_path(models.Project), methods=["POST"])
def create_project() -> dict[str, str]:
    """Create a new project."""
    try:
        store = get_data_store()
        return store.create_project(models.NewProject.model_validate(request.json)).model_dump(mode="json")
    except Exception:
        _log.exception("Error creating project")
        raise


@dash.hooks.route(create_path(models.Artifact), methods=["POST"])
def log_artifact() -> dict[str, str]:
    """Log an artifact with metadata and files."""
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
    """Download an artifact at a specified url."""
    try:
        return get_artifact_store().download_artifact(
            AnyUrl(artifact_url.replace("=", "/").replace("+", ":"))
        )
    except Exception:
        _log.exception("failed to load artifact")
        raise
