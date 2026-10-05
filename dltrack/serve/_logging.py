"""
The structlog configuration every dltrack process needs, wherever a log call might see a traceback.

Not called once at import time -- it has to run again in every process that doesn't inherit
whichever process called it first: `multiprocessing`'s `spawn` start method (used throughout, see
`dltrack._mp_context`) always starts a fresh interpreter with none of the parent's structlog setup,
and Granian's own worker processes re-import `dltrack.serve._wsgi` from scratch for the same
reason. Each such entrypoint calls this once, before its first log call.
"""

from __future__ import annotations

import logging
import sys
from enum import StrEnum

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogFormat(StrEnum):
    """How a log line is rendered."""

    CONSOLE = "console"
    """Human-readable, colored when stdout is a terminal -- the default, for a developer or a
    plain `docker logs`/journal reader."""
    JSON = "json"
    """One JSON object per line, for a log collector (Loki, CloudWatch, ...) to parse structurally."""


class LoggingSettings(BaseSettings):
    """`DLTRACK_*` environment variables controlling every process's logging."""

    model_config = SettingsConfigDict(env_prefix="DLTRACK_")

    log_level: str = "INFO"
    """A standard `logging` level name (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)."""
    log_format: LogFormat = LogFormat.CONSOLE
    access_log: bool = False
    """Log every request's method, path, status, and duration -- off by default since a reverse
    proxy or load balancer usually already logs this."""


def configure_logging() -> None:
    """
    Log at `DLTRACK_LOG_LEVEL` (default `INFO`), in `DLTRACK_LOG_FORMAT` (default `console`).

    Always renders tracebacks without local variables, in either format: structlog's own
    `RichTracebackFormatter` (console) takes `show_locals` explicitly, and plain
    `traceback.format_exception` (what `format_exc_info` uses for JSON) never includes locals to
    begin with. Otherwise a password, an API token, or (inside a blob-store worker process) cloud
    storage credentials held by a function that then raised would end up in the log.
    """
    settings = LoggingSettings()
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]
    *defaults, _ = structlog.get_config()["processors"]
    match settings.log_format:
        case LogFormat.JSON:
            processors = [
                *defaults,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ]
        case LogFormat.CONSOLE:
            processors = [
                *defaults,
                structlog.dev.ConsoleRenderer(
                    colors=sys.stdout.isatty(),
                    exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
                ),
            ]
    structlog.configure_once(
        processors=processors, wrapper_class=structlog.make_filtering_bound_logger(level)
    )
