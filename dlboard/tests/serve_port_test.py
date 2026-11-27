"""The image's health check asks the port `dlboard serve` listens on: both read `DLBOARD_PORT`, with one default (#810)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlboard import _cli
from dlboard.scripts.healthcheck import HealthCheckSettings


def _served_port(*args: str) -> int:
    _, bound, _ = _cli.app.parse_args(["serve", "custom", "--plugins", "whatever:PLUGINS", *args])
    return bound.arguments["opts"].runtime.port


@given(port=st.none() | st.integers(1, 65535))
def test_the_health_check_asks_the_port_the_server_listens_on(port: int | None) -> None:
    with pytest.MonkeyPatch.context() as env:
        if port is None:
            env.delenv("DLBOARD_PORT", raising=False)
        else:
            env.setenv("DLBOARD_PORT", str(port))

        assert _served_port() == HealthCheckSettings().port


def test_the_command_line_port_outranks_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Which is why a container sets the variable instead: the health check can't see the server's command line."""
    monkeypatch.setenv("DLBOARD_PORT", "9000")

    assert _served_port("--port", "9001") == 9001
