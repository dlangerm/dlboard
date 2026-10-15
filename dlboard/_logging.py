"""
How dlboard's own log calls render a traceback, shared by the client and the server.

structlog's defaults print every frame's local variables -- in a client process that means the
`Authorization` header and the API key of the request that failed -- and up to 100 frames, so a
connection error through `requests`/`urllib3` fills a terminal's scrollback. Nothing here is
installed over a configuration the host application already made.
"""

from __future__ import annotations

import sys

import structlog

SAFE_TRACEBACK = structlog.dev.RichTracebackFormatter(show_locals=False, max_frames=6, extra_lines=1)
"""Never prints local variables, and keeps only the frames nearest the failure."""


def configure_client_logging() -> None:
    """
    Render the client's logged exceptions with `SAFE_TRACEBACK`, unless structlog is already configured.

    A library must not override its host's logging: a training script that set up structlog itself
    keeps its own setup. (Not `configure_once`, which warns on every repeat -- and every logger
    constructed after the first is one.) Every process that logs must call this itself -- the
    shipping processes are `spawn`ed (see `dlboard._mp_context`), so they inherit none of it.
    """
    if structlog.is_configured():
        return
    *defaults, _ = structlog.get_config()["processors"]
    structlog.configure(
        processors=[
            *defaults,
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty(), exception_formatter=SAFE_TRACEBACK),
        ]
    )
