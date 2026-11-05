"""
`/healthz` and `/readyz` -- liveness/readiness for process managers and orchestrators.

Exempt from the request gate like any other `add_public_route` endpoint: a probe has no
credentials and shouldn't need any, and a liveness check in particular has to answer even if
whatever's gating auth (a down DB, under a verifying provider) is itself the problem.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from flask import jsonify
from structlog.stdlib import get_logger

from dlboard.serve._backend._app_slot import current_dash_app
from dlboard.serve._backend._auth import add_public_route
from dlboard.serve._backend._data_store import get_system_data_store
from dlboard.serve._custodian._health import custodial_health
from dlboard.serve._custodian._store import get_system_task_store
from dlboard.serve._custodian._task import registered_tasks

if TYPE_CHECKING:
    from dash import Dash
    from werkzeug.wrappers import Response as BaseResponse

_log = get_logger(__name__)


def _healthz() -> tuple[BaseResponse, int]:
    """The process is up and can handle requests -- no dependency checks."""
    return jsonify(status="ok"), HTTPStatus.OK


def _readyz() -> tuple[BaseResponse, int]:
    """The process is up *and* its data store actually answers a query."""
    try:
        next(iter(get_system_data_store().get_projects()), None)
    except Exception:  # noqa: BLE001 -- a broken store must report 503, not crash the health route itself
        _log.exception("readiness check failed")
        return jsonify(status="unavailable"), HTTPStatus.SERVICE_UNAVAILABLE
    return jsonify(status="ok"), HTTPStatus.OK


def _custodian() -> tuple[BaseResponse, int]:
    """
    Whether background work is being done: a custodian beat lately, and no task has gone three intervals without succeeding.

    Its own route, apart from `/readyz`: a custodian that is down is something to alert on, not a reason to stop sending
    this server requests, which it still serves.
    """
    try:
        app = current_dash_app()
        health = custodial_health(get_system_task_store(app), registered_tasks(app))
    except Exception:  # noqa: BLE001 -- as above: a broken store is a 503
        _log.exception("custodian health check failed")
        return jsonify(status="unavailable"), HTTPStatus.SERVICE_UNAVAILABLE
    status = HTTPStatus.OK if health.healthy else HTTPStatus.SERVICE_UNAVAILABLE
    return (
        jsonify(
            status="ok" if health.healthy else "unavailable",
            custodians=len(health.custodians),
            overdue=health.overdue,
        ),
        status,
    )


def register(app: Dash) -> None:
    """Register `/healthz` (liveness), `/readyz` (readiness) and `/healthz/custodian` (background work), all public."""
    add_public_route(app, "healthz", _healthz, ["GET"])
    add_public_route(app, "readyz", _readyz, ["GET"])
    add_public_route(app, "healthz/custodian", _custodian, ["GET"])
