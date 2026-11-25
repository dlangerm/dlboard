"""
The container's health check: `/healthz` on this machine, over HTTPS when the server serves it (`DLBOARD_SSL_CERTIFICATE`).

A module of its own, run as `python -m dlboard.scripts.healthcheck`, not a `dlboard` command: the CLI loads the whole
server, which takes seconds, and the image gives a check 3. The certificate isn't verified: it is made out to the server's
name, not 127.0.0.1, and a liveness check sends nothing worth keeping private.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import cyclopts
import requests
import urllib3
from pydantic_settings import BaseSettings, SettingsConfigDict

_TIMEOUT_S = 2.0
"""Under the image's `HEALTHCHECK --timeout`, so that a server too slow to answer fails the check rather than hangs it."""


class _Served(BaseSettings):
    """Whether the server in this container serves HTTPS: the variable `TLSSettings` reads, which is too slow to import."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_SSL_")

    certificate: Path | None = None


app = cyclopts.App(name="healthcheck", help="Exit 0 if the server on this machine answers /healthz, else 1.")


@app.default
def check(port: Annotated[int, cyclopts.Parameter(help="The port the server listens on.")] = 8050) -> None:
    """Ask `/healthz`, and exit with whether it answered OK."""
    tls = _Served().certificate is not None
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        answer = requests.get(
            f"{'https' if tls else 'http'}://127.0.0.1:{port}/healthz",
            verify=False,
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException:
        sys.exit(1)
    sys.exit(0 if answer.ok else 1)


if __name__ == "__main__":
    app()
