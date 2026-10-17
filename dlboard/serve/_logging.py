"""
The logging configuration every dlboard server process needs, wherever a log call might see a traceback.

There are two kinds of log call to render alike. dlboard's own go through structlog. Everything else -- Flask and
Werkzeug, SQLAlchemy, Granian itself, any plugin that uses `logging` -- goes through the standard library, which
would print its own format (and, for Granian, its own `[INFO]` lines) next to ours, never in `DLBOARD_LOG_FORMAT`,
without a request id, and out of reach of `DLBOARD_TELEMETRY_LOGS`. `logging_config` sends those through the same
structlog processors, so a line reads the same whichever library wrote it.

Not called once at import time -- it has to run again in every process that doesn't inherit
whichever process called it first: `multiprocessing`'s `spawn` start method (used throughout, see
`dlboard._mp_context`) always starts a fresh interpreter with none of the parent's logging setup,
and Granian's own worker processes re-import `dlboard.serve._wsgi` from scratch for the same
reason. Each such entrypoint calls this once, before its first log call; a process that already has the
configuration (a forked worker inherits its parent's) is left as it is.
"""

from __future__ import annotations

import logging
import logging.config
import sys
import typing
from enum import StrEnum

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard._logging import SAFE_TRACEBACK
from dlboard._telemetry.traces import TraceSettings, trace_ids
from dlboard.serve.telemetry import TelemetrySettings

if typing.TYPE_CHECKING:
    from structlog.typing import EventDict, Processor


class LogFormat(StrEnum):
    """How a log line is rendered."""

    CONSOLE = "console"
    """Human-readable, colored when stdout is a terminal -- the default, for a developer or a
    plain `docker logs`/journal reader."""
    JSON = "json"
    """One JSON object per line, for a log collector (Loki, CloudWatch, ...) to parse structurally."""


class LoggingSettings(BaseSettings):
    """`DLBOARD_*` environment variables controlling every process's logging."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    log_level: str = "INFO"
    """A standard `logging` level name (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)."""
    log_format: LogFormat = LogFormat.CONSOLE
    access_log: bool = False
    """Log every request's method, path, status, and duration -- off by default since a reverse
    proxy or load balancer usually already logs this."""


def _add_trace_ids(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    """Put the open span's ids on the line, so a log line and the trace of its request find each other."""
    if (ids := trace_ids()) is not None:
        event_dict["trace_id"], event_dict["span_id"] = ids
    return event_dict


def _shared_processors() -> list[Processor]:
    """What every event goes through before it is rendered: structlog's own defaults, plus OTLP if it is on."""
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
    ]
    if TraceSettings().traces:
        processors.append(_add_trace_ids)
    if TelemetrySettings().logs:
        from dlboard.serve.telemetry._sdk import (
            forward_log,
        )

        processors.append(forward_log)
    return processors


def _renderers(log_format: LogFormat) -> list[Processor]:
    """What turns an event into a line. Never prints local variables: a traceback with them leaks secrets."""
    match log_format:
        case LogFormat.JSON:
            return [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        case LogFormat.CONSOLE:
            return [
                structlog.dev.ConsoleRenderer(
                    colors=sys.stdout.isatty(),
                    exception_formatter=SAFE_TRACEBACK,
                ),
            ]


def stdlib_formatter() -> structlog.stdlib.ProcessorFormatter:
    """A `logging` formatter that renders a standard-library record like one of dlboard's own events."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*_shared_processors(), structlog.stdlib.add_logger_name],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_renderers(LoggingSettings().log_format),
        ],
    )


def logging_config() -> dict[str, typing.Any]:
    """
    The standard-library `logging` configuration that sends every record through `stdlib_formatter`.

    A plain dict (rather than applied here) so Granian can apply it itself, in its own process and in each
    worker it starts: it is the `log_dictconfig` it takes. dlboard's own loggers and Granian's follow `DLBOARD_LOG_LEVEL`;
    anything else only speaks up from a warning, since at `DEBUG` every library under it would.
    """
    level = LoggingSettings().log_level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"dlboard": {"()": f"{__name__}.{stdlib_formatter.__name__}"}},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "dlboard",
                "stream": "ext://sys.stdout",
            },
            # The same, but only from a warning: a library may set its own logger to `INFO` (Werkzeug does), and a
            # record reaches this handler whatever the root logger's level, so the handler has to do the filtering.
            "libraries": {
                "class": "logging.StreamHandler",
                "formatter": "dlboard",
                "stream": "ext://sys.stdout",
                "level": "WARNING",
            },
        },
        "loggers": {
            "dlboard": {"handlers": ["console"], "level": level, "propagate": False},
            "_granian": {"handlers": ["console"], "level": level, "propagate": False},
        },
        "root": {"handlers": ["libraries"], "level": "WARNING"},
    }


def configure_logging() -> None:
    """
    Log at `DLBOARD_LOG_LEVEL` (default `INFO`), in `DLBOARD_LOG_FORMAT` (default `console`).

    Always renders tracebacks without local variables, in either format: the console's
    `SAFE_TRACEBACK` (also capped in length) takes `show_locals` explicitly, and plain
    `traceback.format_exception` (what `format_exc_info` uses for JSON) never includes locals to
    begin with. Otherwise a password, an API token, or (inside a blob-store worker process) cloud
    storage credentials held by a function that then raised would end up in the log.

    With `DLBOARD_TELEMETRY_LOGS` on, every event is also sent over OTLP (see `dlboard.serve.telemetry`).

    Does nothing in a process that is already configured -- a Granian worker is forked from a parent that did
    this, so it has the setup, and doing it again only repeats work (structlog warns about it).
    """
    if structlog.is_configured():
        return
    level = logging.getLevelNamesMapping()[LoggingSettings().log_level.upper()]
    structlog.configure(
        processors=[*_shared_processors(), *_renderers(LoggingSettings().log_format)],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    logging.config.dictConfig(logging_config())
