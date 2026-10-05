"""
A request id on every log line for the life of a request, plus an opt-in one-line access log.

Independent of auth (see `_auth.py` for the request gate and its own security-header
`after_request`) -- this runs for every request, public routes included, since correlating a
health-check probe or a failed login attempt by request id is exactly as useful as any other.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog
from flask import g, request
from structlog.stdlib import get_logger

from dltrack.serve._logging import LoggingSettings

if TYPE_CHECKING:
    from dash import Dash
    from werkzeug.wrappers import Response as BaseResponse

_log = get_logger(__name__)
_REQUEST_ID_HEADER = "X-Request-Id"


def register(app: Dash) -> None:
    """Bind a request id to every log line for the life of each request; log a summary if enabled."""
    access_log = LoggingSettings().access_log

    @app.server.before_request
    def _start_request() -> None:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        g.dltrack_request_started_at = time.perf_counter()

    @app.server.after_request
    def _finish_request(response: BaseResponse) -> BaseResponse:
        response.headers[_REQUEST_ID_HEADER] = structlog.contextvars.get_contextvars().get("request_id", "")
        if access_log:
            duration_ms = (time.perf_counter() - g.dltrack_request_started_at) * 1000
            _log.info(
                "request",
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=round(duration_ms, 1),
            )
        return response

    @app.server.teardown_request
    def _clear_request_context(_exc: BaseException | None) -> None:
        structlog.contextvars.clear_contextvars()
