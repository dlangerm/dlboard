"""
A request id on every log line for the life of a request, the HTTP request metrics, an opt-in access log, and a log line for every error nobody handled.

Independent of auth (see `_auth.py` for the request gate and its own security-header
`after_request`) -- this runs for every request, public routes included, since correlating a
health-check probe or a failed login attempt by request id is exactly as useful as any other.
`register` has to run after the gate is installed: it puts its `before_request` ahead of the gate's,
so a request the gate turns away (a 401, a redirect to sign in) still has a request id and a start time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from http import HTTPMethod
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

import structlog
from flask import current_app, g, request
from structlog.stdlib import get_logger
from werkzeug.exceptions import HTTPException, InternalServerError

from dlboard._wire import REQUEST_ID_HEADER
from dlboard.serve._logging import LoggingSettings
from dlboard.serve.telemetry import AttributeKey, Attributes, Histogram, Unit, UpDownCounter, attribute

if TYPE_CHECKING:
    from dash import Dash
    from werkzeug.wrappers import Response as BaseResponse

_log = get_logger(__name__)


_Method = HTTPMethod | Literal["_OTHER"]


@dataclass(frozen=True, slots=True)
class _ActiveAttributes(Attributes):
    method: _Method = attribute(AttributeKey.HTTP_REQUEST_METHOD)


@dataclass(frozen=True, slots=True)
class _RequestAttributes(_ActiveAttributes):
    route: str = attribute(AttributeKey.HTTP_ROUTE)
    status_code: int = attribute(AttributeKey.HTTP_RESPONSE_STATUS_CODE)


_ACTIVE = UpDownCounter[_ActiveAttributes](
    "http.server.active_requests", Unit.REQUESTS, "HTTP requests being handled right now."
)
_DURATION = Histogram[_RequestAttributes](
    "http.server.request.duration", Unit.SECONDS, "How long each HTTP request took to answer."
)


def _method() -> _Method:
    """The request's method, or `_OTHER` for one nobody standard sends -- bounded, for a metric label."""
    try:
        return HTTPMethod(request.method)
    except ValueError:
        return "_OTHER"


def register(app: Dash) -> None:
    """Bind a request id to every log line for the life of each request; record and (if enabled) log a summary."""
    access_log = LoggingSettings().access_log

    def _start_request() -> None:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)
        g.dlboard_request_started_at = time.perf_counter()
        _ACTIVE.add(1, _ActiveAttributes(method=_method()))

    app.server.before_request_funcs.setdefault(None, []).insert(0, _start_request)

    @app.server.after_request
    def _finish_request(response: BaseResponse) -> BaseResponse:
        response.headers[REQUEST_ID_HEADER] = structlog.contextvars.get_contextvars().get("request_id", "")
        duration = time.perf_counter() - cast("float", g.dlboard_request_started_at)
        route = request.url_rule.rule if request.url_rule else ""
        _DURATION.record(
            duration,
            _RequestAttributes(method=_method(), route=route, status_code=response.status_code),
        )
        if access_log:
            _log.info(
                "request",
                method=request.method,
                path=request.path,
                status=response.status_code,
                duration_ms=round(duration * 1000, 1),
            )
        return response

    @app.server.teardown_request
    def _clear_request_context(_exc: BaseException | None) -> None:
        if "dlboard_request_started_at" in g:
            _ACTIVE.add(-1, _ActiveAttributes(method=_method()))
        structlog.contextvars.clear_contextvars()

    def _log_unhandled_error(exc: Exception) -> HTTPException:
        """
        Log an exception no route handled, through structlog, then answer with the usual 500.

        Left to Flask it would be logged by the standard library's own logger instead: in no configured format, with
        no request id. A deliberate error response (a 404, a 400 a route aborted with) is not a failure of ours, and in
        debug mode the exception has to keep propagating, to the interactive debugger and Dash's in-browser traceback.
        """
        if isinstance(exc, HTTPException):
            return exc
        if current_app.debug:
            raise exc
        _log.exception("unhandled error", method=request.method, path=request.path)
        return InternalServerError()

    app.server.errorhandler(Exception)(_log_unhandled_error)
