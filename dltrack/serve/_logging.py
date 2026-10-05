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

import structlog


def configure_logging() -> None:
    """
    Log at INFO, with tracebacks that never print local variables.

    structlog's default traceback renderer shows every frame's locals, which is how a password, an
    API token, or (inside a blob-store worker process) cloud storage credentials held by a function
    that then raised would end up in the log.
    """
    *defaults, _ = structlog.get_config()["processors"]
    renderer = structlog.dev.ConsoleRenderer(
        colors=sys.stdout.isatty(),
        exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
    )
    structlog.configure_once(
        processors=[*defaults, renderer], wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )
