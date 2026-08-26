"""
The `dltrack` console command.

Not part of the public plugin-facing API (nothing imports this to build a plugin) -- it's an
entrypoint, installed as a console script by `[project.scripts]` in `pyproject.toml`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import cyclopts
import structlog

from dltrack.plugins.data_stores.filesystem import AppSettings as FilesystemAppSettings
from dltrack.plugins.data_stores.sqlite import AppSettings as SqliteAppSettings
from dltrack.serve import app as build_app
from dltrack.serve import resolve_plugins, run_production_server, set_setting_env

app = cyclopts.App(name="dltrack", help="dltrack: a free, self-hosted experiment-tracking server.")
serve_app = cyclopts.App(name="serve", help="Run the dltrack server against a specific deployment target.")
app.command(serve_app)

_log = structlog.stdlib.get_logger(__name__)

_DEFAULT_SQLITE_LOCATION = Path.home() / ".dltrack.sqlite"
_DEFAULT_ARTIFACT_STORE_LOCATION = Path.home() / ".dltrack_artifacts"

# `dltrack serve local` is just `custom` pointed at dltrack's own built-in plugin list -- resolved
# through the exact same import mechanism as a caller-supplied `--plugins module:attr` target.
_LOCAL_PLUGINS_TARGET = "dltrack.plugins:LOCAL_DEPLOYMENT_DEFAULT"


@dataclass(frozen=True)
class ServerRuntimeOptions:
    """Runtime options shared by every `dltrack serve` command, regardless of its plugin list."""

    debug: Annotated[
        bool, cyclopts.Parameter(help="Run the underlying Dash dev server instead of the production server.")
    ] = False
    host: Annotated[str, cyclopts.Parameter(help="Interface to bind to.")] = "127.0.0.1"
    port: Annotated[int, cyclopts.Parameter(help="Port to listen on.")] = 8050
    workers: Annotated[
        int,
        cyclopts.Parameter(
            help="Worker processes to run (ignored in --debug, which is always single-process)."
        ),
    ] = 2
    blocking_threads: Annotated[
        int,
        cyclopts.Parameter(
            help="Blocking threads per worker for handling requests (ignored in --debug). dltrack is a "
            "sync WSGI app with modest concurrency needs, not a high-throughput API -- left unset, "
            "Granian auto-sizes this off the host's CPU count, which is oversized here and triggers "
            "its own startup warning."
        ),
    ] = 16


def _serve(plugins_target: str, runtime: ServerRuntimeOptions) -> None:
    """
    Serve the plugin list at `plugins_target` ("module.path:attribute"), shared by every `serve` command.

    `--debug` runs Dash's own dev server (auto-reload, in-browser tracebacks) in-process, single-
    worker -- it says itself it's not for production use. Otherwise, Granian: a Rust-based,
    production-ready WSGI server with real multi-worker process isolation (a crashed worker
    doesn't take down others, and can be auto-respawned) and reverse-proxy/k8s-friendly defaults.
    """
    if runtime.debug:
        plugins = resolve_plugins(plugins_target)
        build_app(plugins).run(  # pyright: ignore[reportUnknownMemberType]
            host=runtime.host, port=runtime.port, debug=True
        )
        return
    run_production_server(
        plugins_target,
        host=runtime.host,
        port=runtime.port,
        workers=runtime.workers,
        blocking_threads=runtime.blocking_threads,
    )


@dataclass(frozen=True)
class LocalServeOptions:
    """Options for `dltrack serve local`, grouped into one type so the command signature stays flat."""

    sqlite_location: Annotated[
        Path,
        cyclopts.Parameter(env_var="SQLITE_LOCATION", help="Where to store the sqlite metadata database."),
    ] = _DEFAULT_SQLITE_LOCATION
    artifact_store_location: Annotated[
        Path,
        cyclopts.Parameter(env_var="ARTIFACT_STORE_LOCATION", help="Where to store artifact blobs on disk."),
    ] = _DEFAULT_ARTIFACT_STORE_LOCATION
    runtime: Annotated[ServerRuntimeOptions, cyclopts.Parameter(name="*")] = field(
        default_factory=ServerRuntimeOptions
    )


_DEFAULT_LOCAL_SERVE_OPTIONS = LocalServeOptions()


@serve_app.command
def local(
    *, opts: Annotated[LocalServeOptions, cyclopts.Parameter(name="*")] = _DEFAULT_LOCAL_SERVE_OPTIONS
) -> None:
    """Run the dltrack server locally -- anonymous, single-user, just like `tensorboard`."""
    structlog.configure_once(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    set_setting_env(SqliteAppSettings, "sqlite_location", opts.sqlite_location)
    set_setting_env(FilesystemAppSettings, "artifact_store_location", opts.artifact_store_location)
    _log.info("dltrack server is starting...")
    _serve(_LOCAL_PLUGINS_TARGET, opts.runtime)


@dataclass(frozen=True)
class CustomServeOptions:
    """Options for `dltrack serve custom`, grouped into one type so the command signature stays flat."""

    plugins: Annotated[
        str,
        cyclopts.Parameter(
            env_var="DLTRACK_PLUGINS",
            help='Import path to a `list[PluginProtocol]`, e.g. "myapp.deployment:PLUGINS". Storage, '
            "auth, pages, charts, and themes all come from this list -- dltrack's own "
            "`dltrack.plugins.LOCAL_DEPLOYMENT` is a `list[PluginProtocol]` of the same shape.",
        ),
    ]
    runtime: Annotated[ServerRuntimeOptions, cyclopts.Parameter(name="*")] = field(
        default_factory=ServerRuntimeOptions
    )


@serve_app.command
def custom(*, opts: Annotated[CustomServeOptions, cyclopts.Parameter(name="*")]) -> None:
    """Run the dltrack server against a caller-supplied plugin list -- storage, auth, and all."""
    structlog.configure_once(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    _log.info("dltrack server is starting...")
    _serve(opts.plugins, opts.runtime)


def main() -> None:
    """Entrypoint for the installed `dltrack` console script."""
    app()


if __name__ == "__main__":
    main()
