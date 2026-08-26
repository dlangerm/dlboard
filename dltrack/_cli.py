"""
The `dltrack` console command.

Not part of the public plugin-facing API (nothing imports this to build a plugin) -- it's an
entrypoint, installed as a console script by `[project.scripts]` in `pyproject.toml`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import cyclopts
import structlog

from dltrack.plugins import LOCAL_DEPLOYMENT, themes
from dltrack.plugins.data_stores.filesystem import AppSettings as FilesystemAppSettings
from dltrack.plugins.data_stores.sqlite import AppSettings as SqliteAppSettings
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from pydantic_settings import BaseSettings

app = cyclopts.App(name="dltrack", help="dltrack: a free, self-hosted experiment-tracking server.")
serve_app = cyclopts.App(name="serve", help="Run the dltrack server against a specific deployment target.")
app.command(serve_app)

_log = structlog.stdlib.get_logger(__name__)

_DEFAULT_SQLITE_LOCATION = Path.home() / ".dltrack.sqlite"
_DEFAULT_ARTIFACT_STORE_LOCATION = Path.home() / ".dltrack_artifacts"


def _set_setting_env(settings_cls: type[BaseSettings], field_name: str, value: object) -> None:
    """
    Set the environment variable that `settings_cls`'s pydantic-settings field `field_name` reads.

    Keeps the CLI's env var names coupled to the actual plugin config fields, rather than
    hardcoding env var name strings that could silently drift from what the plugin renamed its
    field to: this raises immediately if `field_name` doesn't exist on `settings_cls`, instead of
    setting an env var nothing reads anymore. Relies on `settings_cls` using pydantic-settings'
    default (no `env_prefix`/per-field alias) env var derivation, `field_name.upper()` -- true for
    both `AppSettings` classes this is used with; see `cli_settings_coupling_test.py`.
    """
    if field_name not in settings_cls.model_fields:
        msg = f"{settings_cls.__qualname__} has no field {field_name!r}"
        raise AttributeError(msg)
    os.environ[field_name.upper()] = str(value)


@serve_app.command
def local(
    *,
    sqlite_location: Annotated[
        Path,
        cyclopts.Parameter(env_var="SQLITE_LOCATION", help="Where to store the sqlite metadata database."),
    ] = _DEFAULT_SQLITE_LOCATION,
    artifact_store_location: Annotated[
        Path,
        cyclopts.Parameter(env_var="ARTIFACT_STORE_LOCATION", help="Where to store artifact blobs on disk."),
    ] = _DEFAULT_ARTIFACT_STORE_LOCATION,
    debug: Annotated[bool, cyclopts.Parameter(help="Run the underlying Dash server in debug mode.")] = False,
) -> None:
    """Run the dltrack server locally -- anonymous, single-user, just like `tensorboard`."""
    structlog.configure_once(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    _set_setting_env(SqliteAppSettings, "sqlite_location", sqlite_location)
    _set_setting_env(FilesystemAppSettings, "artifact_store_location", artifact_store_location)
    _log.info("dltrack server is starting...")
    build_app([*LOCAL_DEPLOYMENT, themes.dark]).run(debug=debug)  # pyright: ignore[reportUnknownMemberType]


def main() -> None:
    """Entrypoint for the installed `dltrack` console script."""
    app()


if __name__ == "__main__":
    main()
