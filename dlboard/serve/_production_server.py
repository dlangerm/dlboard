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

import ssl
from pathlib import Path
from typing import Annotated, Self

from granian import Granian
from granian.constants import Interfaces
from granian.http import HTTP1Settings
from granian.log import LogLevels
from pydantic import FilePath, ImportString, NonNegativeInt, PositiveInt, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from dlboard.models import PluginProtocol
from dlboard.serve._custodian._process import CustodianProcess
from dlboard.serve._logging import LoggingSettings, logging_config
from dlboard.serve._settings_env import set_setting_env
from dlboard.serve.telemetry import NoAttributes, Unit, UpDownCounter, shared_prometheus_dir

WORKERS = UpDownCounter[NoAttributes](
    "dlboard.server.workers", Unit.WORKERS, "Server worker processes that have started and are serving."
)
"""Counted by `_wsgi` once a worker has built its app. Defined here because `_wsgi` builds the app as it is imported."""


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


class TLSSettings(BaseSettings):
    """
    `DLBOARD_SSL_*`: serve HTTPS from a certificate and key, with no reverse proxy in front (#797).

    Both or neither: half a pair is a startup error, never a silent fall back to plain HTTP. With them, the port answers
    HTTPS only (HTTP/2 or HTTP/1.1, as the client offers), and plain HTTP to it is refused.
    """

    model_config = SettingsConfigDict(env_prefix="DLBOARD_SSL_")

    certificate: FilePath | None = None
    """The certificate, with its chain, as PEM."""
    keyfile: FilePath | None = None
    """
    Its private key, as PEM, unencrypted (as `tailscale cert` and certbot write it): Granian can't read the encrypted keys
    OpenSSL writes, so the file's permissions are what keep it private.
    """

    @model_validator(mode="after")
    def _a_pair_that_loads(self) -> Self:
        match self.certificate, self.keyfile:
            case None, None:
                return self
            case Path() as certificate, Path() as keyfile:
                # Loaded once here, where a key that isn't the certificate's, or is encrypted, can say so: a worker that
                # can't load it panics, and the server exits with nothing else in its log. The password callback
                # stands in for OpenSSL's, which would ask for an encrypted key's password on the terminal.
                try:
                    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(
                        certificate, keyfile, password=lambda: ""
                    )
                except ssl.SSLError as error:
                    msg = (
                        f"DLBOARD_SSL_CERTIFICATE and DLBOARD_SSL_KEYFILE don't load as a pair ({error.reason}): "
                        "the key must be the certificate's own, and unencrypted."
                    )
                    raise ValueError(msg) from error
                return self
            case _:  # half a pair
                missing = "DLBOARD_SSL_CERTIFICATE" if self.certificate is None else "DLBOARD_SSL_KEYFILE"
                msg = f"Native TLS needs a certificate and its key: set {missing} too, or unset the other."
                raise ValueError(msg)

    @property
    def enabled(self) -> bool:
        return self.certificate is not None


def resolve_plugins(target: str) -> list[PluginProtocol]:
    """Import and validate the `list[PluginProtocol]` at `target`, e.g. `"myapp.deployment:PLUGINS"`."""
    return WSGISettings(dlboard_plugins=target).dlboard_plugins


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
    tuning, tls = GranianSettings(), TLSSettings()
    granian = Granian(
        "dlboard.serve._wsgi:app",
        address=host,
        port=port,
        interface=Interfaces.WSGI,
        workers=workers,
        blocking_threads=blocking_threads,
        respawn_failed_workers=True,
        # Granian's own lines (and, in each worker, every standard-library logger) in the format everything else uses.
        log_level=LogLevels(LoggingSettings().log_level.lower()),
        log_dictconfig=logging_config(),
        workers_kill_timeout=tuning.workers_kill_timeout_s,
        workers_max_rss=tuning.workers_max_rss_mb or None,
        backpressure=tuning.backpressure or None,
        http1_settings=HTTP1Settings(
            header_read_timeout=tuning.header_read_timeout_s * 1000, keep_alive=tuning.keep_alive
        ),
        # Read by each worker as it starts, so a renewed certificate is served once the workers restart (SIGHUP).
        ssl_cert=tls.certificate,
        ssl_key=tls.keyfile,
    )
    # The custodian runs the background tasks in a process of its own, started before the workers and stopped after
    # them by this one (the master), never by a worker: see `dlboard/serve/_custodian/_process.py`.
    custodian = CustodianProcess(stop_timeout_s=tuning.workers_kill_timeout_s)
    granian.on_startup(custodian.start)
    granian.on_shutdown(custodian.stop)
    # The workers and the custodian (and what they spawn) inherit the directory they leave their Prometheus snapshots in.
    with shared_prometheus_dir():
        granian.serve()
