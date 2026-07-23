"""Routes for doing general api things."""

import dash
from flask import request
from structlog.stdlib import get_logger

from dltrack.models import Experiment, LoggedMetrics, NewExperiment
from dltrack.plugins.utilities import get_data_store

_log = get_logger(__name__)


@dash.hooks.route("/log-batch", methods=["POST"])
def func() -> dict[str, str]:
    try:
        store = get_data_store()
        store.log_metrics(LoggedMetrics.model_validate(metric) for metric in request.json)
    except Exception:
        _log.exception("Error logging metrics")
        raise
    return {}


@dash.hooks.route(f"/create/{Experiment.__name__}", methods=["POST"])
def create_experiment() -> dict[str, str]:
    try:
        store = get_data_store()
        exp = store.create_experiment(NewExperiment.model_validate(request.json))
        assert exp
        return exp.model_dump(mode="json")
    except Exception:
        _log.exception("Error creating")
        raise
