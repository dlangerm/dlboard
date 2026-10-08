"""
Running the composed dlboard app under a production WSGI server (Granian).

Deliberately has no knowledge of any specific plugin list -- `run_production_server` takes an
import path to whatever `list[PluginProtocol]` the caller wants, so the same code path serves
`dlboard`'s own `LOCAL_DEPLOYMENT` and a third party's custom plugin set. `WSGISettings` does the
actual resolution: pydantic's `ImportString` imports the path, and (since `PluginProtocol` is
`@runtime_checkable`) validates each element against it directly -- a real validation error if the
path doesn't resolve, or resolves to something that isn't plugin-shaped.
"""

from __future__ import annotations

from typing import Annotated

from granian import Granian
from granian.constants import Interfaces
from granian.http import HTTP1Settings
from pydantic import ImportString, NonNegativeInt, PositiveInt
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from dlboard.models import PluginProtocol
from dlboard.serve._settings_env import set_setting_env


class WSGISettings(BaseSettings):
    """Resolves the plugin list (and URL prefix) `dlboard.serve._wsgi:app` builds the production app from."""

    dlboard_plugins: Annotated[ImportString[list[PluginProtocol]], NoDecode]
    """Import path to a `list[PluginProtocol]`, e.g. `"dlboard.plugins:LOCAL_DEPLOYMENT"`."""
    dlboard_url_prefix: str = ""
    """See `dlboard.serve.app.app`'s `url_prefix` -- `DLBOARD_URL_PREFIX` from the CLI."""


class GranianSettings(BaseSettings):
    """
    `DLBOARD_*` environment variables tuning the Granian process itself.

    Separate from `WSGISettings`: Granian's own parent process reads these before any worker exists,
    while `WSGISettings` is read by each worker to build the app (and needs the plugin list to do it).
    """

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    workers_kill_timeout_s: PositiveInt = 30
    """
    How long a worker gets to exit after SIGTERM (a restart, deploy, or `docker stop`) before it is killed.

    In-flight requests finish and queued artifact blobs are written and recorded within this window;
    anything still unfinished is abandoned. Keep it below the orchestrator's own grace period
    (Kubernetes' `terminationGracePeriodSeconds` and Docker's `--stop-timeout` default to 30 and 10 seconds).
    """
    workers_max_rss_mb: NonNegativeInt = 0
    """Restart a worker (gracefully, one at a time) once its memory use passes this many MB. `0` disables it."""
    backpressure: NonNegativeInt = 0
    """Max requests each worker accepts at once before new connections wait in the socket backlog. `0` lets Granian choose."""
    header_read_timeout_s: PositiveInt = 30
    """How long a client may take to send its request headers; guards against slowloris-style connections."""
    keep_alive: bool = True
    """Keep HTTP/1 connections open between requests. Turn off if a proxy in front mishandles idle connections."""


def resolve_plugins(target: str) -> list[PluginProtocol]:
    """Import and validate the `list[PluginProtocol]` at `target`, e.g. `"myapp.deployment:PLUGINS"`."""
    return WSGISettings(dlboard_plugins=target).dlboard_plugins  # pyright: ignore[reportArgumentType]


def run_production_server(  # noqa: PLR0913
    plugins_target: str, *, host: str, port: int, workers: int, blocking_threads: int, url_prefix: str = ""
) -> None:
    """
    Serve the app built from the plugin list at `plugins_target` under Granian.

    Granian spawns each worker as a separate process that re-imports `dlboard.serve._wsgi:app`, so
    `plugins_target`/`url_prefix` are handed off via the env vars `WSGISettings` reads, rather than
    as live objects -- see `dlboard/serve/_wsgi.py`.
    """
    set_setting_env(WSGISettings, "dlboard_plugins", plugins_target)
    set_setting_env(WSGISettings, "dlboard_url_prefix", url_prefix)
    tuning = GranianSettings()
    Granian(
        "dlboard.serve._wsgi:app",
        address=host,
        port=port,
        interface=Interfaces.WSGI,
        workers=workers,
        blocking_threads=blocking_threads,
        respawn_failed_workers=True,
        workers_kill_timeout=tuning.workers_kill_timeout_s,
        workers_max_rss=tuning.workers_max_rss_mb or None,
        backpressure=tuning.backpressure or None,
        http1_settings=HTTP1Settings(
            header_read_timeout=tuning.header_read_timeout_s * 1000, keep_alive=tuning.keep_alive
        ),
    ).serve()
