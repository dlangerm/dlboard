"""
The container's health check: `/healthz` on this machine, on the port `dlboard serve` listens on, over HTTPS when it serves it.

A module of its own, run as `python -m dlboard.scripts.healthcheck`, not a `dlboard` command: the CLI loads the whole
server, which takes seconds, and the image gives a check 3. The certificate isn't verified: it is made out to the server's
name, not 127.0.0.1, and a liveness check sends nothing worth keeping private.

The check runs as a process of its own, with the container's environment, not the server's: what it knows of how the
server serves is what that environment says, so the port is `DLBOARD_PORT`, which `dlboard serve --port` reads too.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import requests
import urllib3
from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PORT: Final = 8050
"""The port `dlboard serve` listens on, and this asks, with no `DLBOARD_PORT`."""

_TIMEOUT_S: Final = 2.0
"""Under the image's `HEALTHCHECK --timeout`, so that a server too slow to answer fails the check rather than hangs it."""


class HealthCheckSettings(BaseSettings):
    """How the server in this container serves: the variables it reads, without importing it (too slow for the check)."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    port: PositiveInt = DEFAULT_PORT
    """The port `dlboard serve` listens on, unless its `--port` says otherwise."""
    ssl_certificate: Path | None = None
    """Set when it serves HTTPS (`TLSSettings`)."""


def main() -> None:
    """Ask `/healthz`, and exit 0 if it answered OK, else 1."""
    served = HealthCheckSettings()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        answer = requests.get(
            f"{'http' if served.ssl_certificate is None else 'https'}://127.0.0.1:{served.port}/healthz",
            verify=False,
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        sys.exit(1)
    sys.exit(0 if answer.ok else 1)


if __name__ == "__main__":
    main()
