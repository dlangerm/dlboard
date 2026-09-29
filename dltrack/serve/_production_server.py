"""
Running the composed dltrack app under a production WSGI server (Granian).

Deliberately has no knowledge of any specific plugin list -- `run_production_server` takes an
import path to whatever `list[PluginProtocol]` the caller wants, so the same code path serves
`dltrack`'s own `LOCAL_DEPLOYMENT` and a third party's custom plugin set. `WSGISettings` does the
actual resolution: pydantic's `ImportString` imports the path, and (since `PluginProtocol` is
`@runtime_checkable`) validates each element against it directly -- a real validation error if the
path doesn't resolve, or resolves to something that isn't plugin-shaped.
"""

from __future__ import annotations

from typing import Annotated

from granian import Granian
from granian.constants import Interfaces
from pydantic import ImportString
from pydantic_settings import BaseSettings, NoDecode

from dltrack.models import PluginProtocol
from dltrack.serve._settings_env import set_setting_env


class WSGISettings(BaseSettings):
    """Resolves the plugin list `dltrack.serve._wsgi:app` builds the production app from."""

    dltrack_plugins: Annotated[ImportString[list[PluginProtocol]], NoDecode]
    """Import path to a `list[PluginProtocol]`, e.g. `"dltrack.plugins:LOCAL_DEPLOYMENT"`."""


def resolve_plugins(target: str) -> list[PluginProtocol]:
    """Import and validate the `list[PluginProtocol]` at `target`, e.g. `"myapp.deployment:PLUGINS"`."""
    return WSGISettings(dltrack_plugins=target).dltrack_plugins  # pyright: ignore[reportArgumentType]


def run_production_server(
    plugins_target: str, *, host: str, port: int, workers: int, blocking_threads: int
) -> None:
    """
    Serve the app built from the plugin list at `plugins_target` under Granian.

    Granian spawns each worker as a separate process that re-imports `dltrack.serve._wsgi:app`, so
    `plugins_target` is handed off via the env var `WSGISettings` reads, rather than as a live
    object -- see `dltrack/serve/_wsgi.py`.
    """
    set_setting_env(WSGISettings, "dltrack_plugins", plugins_target)
    Granian(
        "dltrack.serve._wsgi:app",
        address=host,
        port=port,
        interface=Interfaces.WSGI,
        workers=workers,
        blocking_threads=blocking_threads,
        respawn_failed_workers=True,
    ).serve()
