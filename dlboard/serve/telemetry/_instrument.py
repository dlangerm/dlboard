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
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from opentelemetry.instrumentation.flask import FlaskInstrumentor
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from structlog.stdlib import get_logger

if TYPE_CHECKING:
    from importlib.metadata import EntryPoint
    from typing import Any

    from flask import Flask

    from dlboard._telemetry._sdk import Traces

_log = get_logger(__name__)

_ENTRY_POINT_GROUP: Final = "opentelemetry_instrumentor"
_PER_APP: Final = "flask"
"""Instrumented by `instrument_flask`, not by the process-wide pass."""
_ANY_VERSION: Final = frozenset({"sqlalchemy"})
"""
Instrumentors whose declared range of the library is not checked.

dlboard allows any SQLAlchemy 2.x, and the instrumentor caps its range below the newest release for months at a time,
which would silently switch off every SQL span. It only listens to Core's cursor events, which are stable;
`server_traces_test.py` fails if a SQLAlchemy release ever breaks that.
"""

_UNTRACED_URLS: Final = (
    "healthz",
    "readyz",
    "metrics",
    "_dash-component-suites",
    "_favicon.ico",
    "_reload-hash",
    r"\.[0-9a-f]{8}\.(?:css|js|woff2)$",  # an asset `serve_asset` serves under a content-hashed name
)
"""Requests nobody looks at in a trace: health probes, scrapes, and static files."""


class InstrumentationSettings(BaseSettings):
    """The standard `OTEL_PYTHON_*` variables that say what to instrument."""

    model_config = SettingsConfigDict(env_prefix="OTEL_PYTHON_")

    disabled_instrumentations: Annotated[frozenset[str], NoDecode] = frozenset()
    """Comma-separated instrumentor names (`sqlalchemy`, `psycopg`, `botocore`, ...) not to run."""
    flask_excluded_urls: str = ""
    """Comma-separated regular expressions of request URLs not to trace, besides dlboard's own."""

    @field_validator("disabled_instrumentations", mode="before")
    @classmethod
    def _split_commas(cls, value: Any) -> Any:  # noqa: ANN401 -- a `mode="before"` validator sees the raw input
        return [v.strip() for v in value.split(",") if v.strip()] if isinstance(value, str) else value


class _Instrumentor(Protocol):
    """What every `BaseInstrumentor` offers that is used here."""

    @property
    def is_instrumented_by_opentelemetry(self) -> bool: ...

    def instrument(self, **kwargs: object) -> None: ...

    def uninstrument(self, **kwargs: object) -> None: ...


def _process_wide() -> list[EntryPoint]:
    """The installed instrumentors that patch a library for the whole process, which is all but Flask's."""
    return [e for e in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP) if e.name != _PER_APP]


def instrument_libraries(traces: Traces) -> None:
    """Instrument every installed library that has an instrumentor, once per process."""
    disabled = InstrumentationSettings().disabled_instrumentations
    for entry_point in _process_wide():
        if entry_point.name in disabled:
            continue
        try:
            instrumentor: _Instrumentor = entry_point.load()()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(
                    tracer_provider=traces.provider, skip_dep_check=entry_point.name in _ANY_VERSION
                )
        except Exception:  # noqa: BLE001 -- one library's broken instrumentation must not cost the others, or the server
            _log.warning("could not instrument %s; going on without it", entry_point.name, exc_info=True)


def uninstrument_libraries() -> None:
    """Undo `instrument_libraries`, so a test (or a reload) can start again with another provider."""
    for entry_point in _process_wide():
        instrumentor: _Instrumentor = entry_point.load()()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def instrument_flask(server: Flask, traces: Traces) -> None:
    """Trace `server`'s requests: continue an incoming `traceparent`, name the span by route, record status and errors."""
    excluded = InstrumentationSettings().flask_excluded_urls
    FlaskInstrumentor().instrument_app(
        server,
        tracer_provider=traces.provider,
        excluded_urls=",".join([*_UNTRACED_URLS, *([excluded] if excluded else [])]),
    )
