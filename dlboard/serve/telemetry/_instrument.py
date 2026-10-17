"""
Instrument the libraries a server process uses -- imported only once tracing is switched on.

Unlike a training script, a dlboard server process is dlboard's alone, so patching its libraries is safe, and it is
what keeps the traces honest where dlboard cannot see: the database driver's round trips, every call boto makes to S3,
whatever a plugin's own storage or auth client does. Hand-made spans would stop at dlboard's own boundaries.

- **Libraries** go through the standard `opentelemetry_instrumentor` entry points, the mechanism
  `opentelemetry-instrument` uses: whatever instrumentation package is installed runs, so a plugin that talks to some
  other database gets traced by installing that library's package, with no change here. Each does nothing when its
  library is not installed, and `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` switches any of them off. The SQLAlchemy one
  wraps `create_engine` for the whole process, so it has to run before a store creates its engine, and then covers
  every engine, a plugin's included.
- **Flask** is the exception, instrumented per app: Dash does `from flask import Flask` when it is imported, before
  anything could patch the class, so only the app's own server can be wrapped.

Every instrumentor is handed this process's private tracer provider, never a global one.
"""

from __future__ import annotations

import importlib.metadata
import re
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from opentelemetry.instrumentation.dependencies import get_dependency_conflicts
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard.serve._settings_env import set_setting_env

if TYPE_CHECKING:
    from collections.abc import Collection
    from importlib.metadata import EntryPoint
    from typing import Any

    from flask import Flask

    from dlboard._telemetry._sdk import Traces

_log = get_logger(__name__)

_ENTRY_POINT_GROUP: Final = "opentelemetry_instrumentor"
_SQLALCHEMY: Final = "sqlalchemy"
_DRIVERS: Final = frozenset({"psycopg", "psycopg2"})
"""
Database drivers whose instrumentation traces every statement a second time.

Every statement dlboard runs goes through SQLAlchemy, whose instrumentation already traces it, on SQLite as on Postgres.
A driver's would add a second span for the same statement under the first. So they are left off while SQLAlchemy's runs;
`OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=sqlalchemy` turns SQLAlchemy's off and the driver's on, for traces at the driver
(its connections and cursors) instead.
"""
_PER_APP: Final = "flask"
_FLASK_INSTRUMENTATION: Final = "opentelemetry.instrumentation.flask"
"""The module the request hook Flask's instrumentation registers comes from."""
"""Instrumented by `instrument_flask`, not by the process-wide pass."""
_ANY_VERSION: Final = frozenset({"sqlalchemy"})
"""
Instrumentors whose declared range of the library is not checked.

dlboard allows any SQLAlchemy 2.x, and the instrumentor caps its range below the newest release for months at a time,
which would silently switch off every SQL span. It only listens to Core's cursor events, which are stable;
`server_traces_test.py` fails if a SQLAlchemy release ever breaks that.
"""


_PROBE_ROUTES: Final = ("healthz", "readyz", "metrics")
"""
Routes at the root of the app that nobody looks at in a trace: health probes and the Prometheus scrape.

Matched as whole paths: the name alone would also match the metric ingest route, `api/v1/metrics`.
"""

_STATIC_URLS: Final = (
    "_dash-component-suites",
    "_favicon.ico",
    "_reload-hash",
    r"\.[0-9a-f]{8}\.(?:css|js|woff2)$",  # an asset `serve_asset` serves under a content-hashed name
)
"""Static files, which would drown every page load in spans."""


class InstrumentationSettings(BaseSettings):
    """The standard `OTEL_PYTHON_*` variables that say what to instrument."""

    model_config = SettingsConfigDict(env_prefix="OTEL_PYTHON_")

    disabled_instrumentations: Annotated[frozenset[str], NoDecode] = frozenset()
    """Comma-separated instrumentor names (`sqlalchemy`, `psycopg`, `botocore`, ...) not to run."""
    excluded_urls: str = ""
    """Comma-separated regular expressions of request URLs not to trace, besides dlboard's own."""
    flask_excluded_urls: str = ""
    """For Flask alone; when set it replaces `excluded_urls`, as OpenTelemetry's own instrumentation reads the two."""

    @field_validator("disabled_instrumentations", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value


class HeaderCaptureSettings(BaseSettings):
    """The standard variables for recording HTTP headers on spans -- which dlboard never does unless asked."""

    model_config = SettingsConfigDict(env_prefix="OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_")

    sanitize_fields: str | None = None
    """Comma-separated header names whose values are replaced with `[REDACTED]` if a user records headers."""


_ALWAYS_SANITIZED: Final = "Authorization,Cookie,Set-Cookie"
"""What `sanitize_fields` is, unless the user says otherwise: the headers a credential travels in."""


class SemconvSettings(BaseSettings):
    """The standard variable that picks which generation of HTTP attribute names the instrumentation writes."""

    model_config = SettingsConfigDict(env_prefix="OTEL_SEMCONV_STABILITY_")

    opt_in: str | None = None
    """Comma-separated opt-ins (`http`, `database`, ...). Unset, Flask's instrumentation writes the pre-stable names."""


_STABLE_HTTP: Final = "http"
"""
What `opt_in` is, unless the user says otherwise: the stable HTTP names (`http.request.method`, ...).

The client writes the stable names. With Flask's instrumentation on the old ones (`http.method`) one trace would speak
two vocabularies, and a query or a span-metrics dimension on either would find only half of it.
"""


def _set_env_defaults() -> None:
    """
    Fill in the standard variables dlboard wants a different default for, unless the user set them.

    Read by the instrumentation the first time it is used in the process (the semantic conventions one only once),
    so this runs before the first instrumentor does.
    """
    # Headers are not recorded unless the user asks (`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`); if they do, a
    # credential in one must not leave the server.
    if HeaderCaptureSettings().sanitize_fields is None:
        set_setting_env(HeaderCaptureSettings, "sanitize_fields", _ALWAYS_SANITIZED)
    if SemconvSettings().opt_in is None:
        set_setting_env(SemconvSettings, "opt_in", _STABLE_HTTP)


class _Instrumentor(Protocol):
    """What every `BaseInstrumentor` offers that is used here."""

    @property
    def is_instrumented_by_opentelemetry(self) -> bool: ...

    def instrumentation_dependencies(self) -> Collection[str]: ...

    def instrument(self, **kwargs: object) -> None: ...

    def uninstrument(self, **kwargs: object) -> None: ...


def _process_wide() -> list[EntryPoint]:
    """The installed instrumentors that patch a library for the whole process, which is all but Flask's."""
    return [e for e in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP) if e.name != _PER_APP]


def _library_is_usable(name: str, instrumentor: _Instrumentor) -> bool:
    """
    Whether the library `instrumentor` is for is installed, and in a version it supports (or any, for `_ANY_VERSION`).

    Asked here, as `opentelemetry-instrument` does, rather than leaving it to `instrument()`: that logs an error for
    every instrumentor whose library is not installed, which is most of them, in every worker. Asked of the
    instrumentor, not its distribution: one distribution can hold several (botocore's holds aiobotocore's too).
    """
    conflict = get_dependency_conflicts(instrumentor.instrumentation_dependencies())
    if conflict is None:
        return True
    _log.debug("not instrumenting %s: %s", name, conflict)
    return conflict.found is not None and name in _ANY_VERSION


def instrument_libraries(traces: Traces) -> None:
    """Instrument every installed library that has an instrumentor, once per process."""
    _set_env_defaults()
    disabled = InstrumentationSettings().disabled_instrumentations
    wanted = [e for e in _process_wide() if e.name not in disabled]
    sqlalchemy_traces = False
    for entry_point in sorted(
        wanted, key=lambda e: e.name in _DRIVERS
    ):  # SQLAlchemy first: drivers depend on it
        if entry_point.name in _DRIVERS and sqlalchemy_traces:
            continue
        try:
            instrumentor: _Instrumentor = entry_point.load()()
            if not _library_is_usable(entry_point.name, instrumentor):
                continue
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(tracer_provider=traces.provider, skip_dep_check=True)  # checked above
            sqlalchemy_traces = sqlalchemy_traces or entry_point.name == _SQLALCHEMY
        except Exception:  # noqa: BLE001 -- one library's broken instrumentation must not cost the others, or the server
            _log.warning("could not instrument %s; going on without it", entry_point.name, exc_info=True)


def uninstrument_libraries() -> None:
    """Undo `instrument_libraries`, so a test (or a reload) can start again with another provider."""
    for entry_point in _process_wide():
        instrumentor: _Instrumentor = entry_point.load()()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def instrument_flask(server: Flask, traces: Traces, url_prefix: str) -> None:
    """
    Trace `server`'s requests, served under `url_prefix`: continue an incoming `traceparent`, name the span by route.

    Status and errors are recorded too. A URL is excluded if it matches one of the (comma-separated) regular
    expressions Flask's instrumentation is given, searched for anywhere in the full URL, so each is anchored here.
    """
    root = rf"^https?://[^/]+{re.escape(url_prefix.rstrip('/'))}/"
    settings = InstrumentationSettings()
    excluded = settings.flask_excluded_urls or settings.excluded_urls
    _set_env_defaults()
    FlaskInstrumentor().instrument_app(
        server,
        tracer_provider=traces.provider,
        excluded_urls=",".join(
            [
                *(rf"{root}{route}(?:\?.*)?$" for route in _PROBE_ROUTES),
                *_STATIC_URLS,
                *([excluded] if excluded else []),
            ]
        ),
    )
    # The request's span has to be open before anything else looks at the request. The auth gate runs ahead of the
    # hooks registered before it, so a request it turns away would have no span at all, and the database lookups it
    # makes to find out who is asking would be traces of their own. Flask's instrumentation keeps its hook on the app,
    # whether it was just added or the app's class was already instrumented (a platform's auto-instrumentation), and is found by the module it comes from.
    hooks = server.before_request_funcs.setdefault(None, [])
    opens_the_span = [h for h in hooks if h.__module__.startswith(_FLASK_INSTRUMENTATION)]
    hooks[:] = [*opens_the_span, *(h for h in hooks if h not in opens_the_span)]
