"""
Waiting out a dependency that is still coming up, for a plugin that needs it to start.

A host that has just booted brings its containers up in no particular order, and Compose's
`depends_on: service_healthy` only orders `docker compose up`, not that. A server that gives up on
its first refused connection then dies within seconds of a database that would have answered a few
seconds later, and Granian takes the quick second death for a crash loop and exits. So a plugin
probes what it needs through `wait_for`, which keeps trying for `DLBOARD_STARTUP_WAIT_S` while the
failure is one that waiting can fix, and says so on the way (a log line and a trace event per retry).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import NonNegativeFloat
from pydantic_settings import BaseSettings, SettingsConfigDict
from structlog.stdlib import get_logger

from dlboard._telemetry import Attributes, attribute
from dlboard._telemetry.traces import SpanKind, TraceKey, span

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger(__name__)

_FIRST_DELAY_S: Final = 1.0
_LAST_DELAY_S: Final = 10.0


class StartupSettings(BaseSettings):
    """`DLBOARD_*` environment variables for how a server process starts."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    startup_wait_s: NonNegativeFloat = 120
    """
    How long to keep retrying a dependency that can't be reached yet (the database, the object store) before giving up.

    `0` fails on the first error. Only "not reachable yet" is retried; a wrong password or a missing bucket fails at once.
    """


class Dependency(StrEnum):
    """What a plugin is waiting for."""

    DATABASE = "database"
    OBJECT_STORE = "object store"


@dataclass(frozen=True, slots=True)
class _Waiting(Attributes):
    dependency: Dependency = attribute(TraceKey.STARTUP_DEPENDENCY)


@dataclass(frozen=True, slots=True)
class _Retry(_Waiting):
    attempt: int = attribute(TraceKey.STARTUP_ATTEMPT)


def wait_for[R](
    dependency: Dependency, attempt: Callable[[], R], *, transient: Callable[[Exception], bool]
) -> R:
    """
    Call `attempt()` until it succeeds, retrying while it raises an error `transient` accepts.

    Retries back off from one second to ten, for up to `DLBOARD_STARTUP_WAIT_S` in all. Any other error, or a
    transient one once that time is up, propagates unchanged.
    """
    deadline = time.monotonic() + StartupSettings().startup_wait_s
    delay_s = _FIRST_DELAY_S
    number = 0
    with span("dlboard.startup.wait", SpanKind.INTERNAL, _Waiting(dependency)) as active:
        while True:
            number += 1
            try:
                return attempt()
            except Exception as exc:
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0 or not transient(exc):
                    raise
                wait_s = min(delay_s, remaining_s)
                active.event("retry", _Retry(dependency, number))
                _log.warning(
                    f"{dependency} not reachable yet; retrying",
                    attempt=number,
                    retry_in_s=wait_s,
                    error=str(exc),
                )
                time.sleep(wait_s)
                delay_s = min(delay_s * 2, _LAST_DELAY_S)
