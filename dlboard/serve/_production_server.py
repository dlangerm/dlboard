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
from pydantic import ImportString
from pydantic_settings import BaseSettings, NoDecode

from dlboard.models import PluginProtocol
from dlboard.serve._settings_env import set_setting_env


class WSGISettings(BaseSettings):
    """Resolves the plugin list (and URL prefix) `dlboard.serve._wsgi:app` builds the production app from."""

    dlboard_plugins: Annotated[ImportString[list[PluginProtocol]], NoDecode]
    """Import path to a `list[PluginProtocol]`, e.g. `"dlboard.plugins:LOCAL_DEPLOYMENT"`."""
    dlboard_url_prefix: str = ""
    """See `dlboard.serve.app.app`'s `url_prefix` -- `DLBOARD_URL_PREFIX` from the CLI."""


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
    Granian(
        "dlboard.serve._wsgi:app",
        address=host,
        port=port,
        interface=Interfaces.WSGI,
        workers=workers,
        blocking_threads=blocking_threads,
        respawn_failed_workers=True,
    ).serve()
