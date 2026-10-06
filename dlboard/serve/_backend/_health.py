"""
`/healthz` and `/readyz` -- liveness/readiness for process managers and orchestrators.

Exempt from the request gate like any other `add_public_route` endpoint: a probe has no
credentials and shouldn't need any, and a liveness check in particular has to answer even if
whatever's gating auth (a down DB, under a verifying provider) is itself the problem.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from dash import get_app
from flask import jsonify
from structlog.stdlib import get_logger

from dlboard.serve._backend._auth import add_public_route
from dlboard.serve._backend._data_store import get_system_data_store

if TYPE_CHECKING:
    from dash import Dash
    from werkzeug.wrappers import Response as BaseResponse

_log = get_logger(__name__)


def _healthz() -> tuple[BaseResponse, int]:
    """The process is up and can handle requests -- no dependency checks."""
    return jsonify(status="ok"), HTTPStatus.OK


def _readyz() -> tuple[BaseResponse, int]:
    """The process is up *and* its data store actually answers a query."""
    app = cast("Dash", get_app())
    try:
        next(iter(get_system_data_store(app).get_projects()), None)
    except Exception:  # noqa: BLE001 -- a broken store must report 503, not crash the health route itself
        _log.exception("readiness check failed")
        return jsonify(status="unavailable"), HTTPStatus.SERVICE_UNAVAILABLE
    return jsonify(status="ok"), HTTPStatus.OK


def register(app: Dash) -> None:
    """Register `/healthz` (liveness) and `/readyz` (readiness), both public."""
    add_public_route(app, "healthz", _healthz, ["GET"])
    add_public_route(app, "readyz", _readyz, ["GET"])
