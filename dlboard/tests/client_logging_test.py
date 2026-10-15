"""A client process logs failures without printing the locals of the frames they passed through, and briefly."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from dlboard._logging import configure_client_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def _recurse(depth: int, api_key: str) -> None:
    if depth == 0:
        msg = "boom"
        raise ValueError(msg)
    _recurse(depth - 1, api_key)


def test_a_logged_client_failure_is_short_and_keeps_secrets_out(capsys: pytest.CaptureFixture[str]) -> None:
    configure_client_logging()
    secret = "-".join(["Bearer", "hunter2"])
    try:
        _recurse(50, secret)
    except ValueError:
        structlog.get_logger().exception("it failed")

    output = capsys.readouterr().out
    assert "boom" in output
    assert secret not in output
    assert len(output.splitlines()) < 60


def test_a_host_applications_own_structlog_setup_is_left_alone(capsys: pytest.CaptureFixture[str]) -> None:
    structlog.configure(processors=[structlog.processors.KeyValueRenderer()])

    configure_client_logging()
    structlog.get_logger().info("hello")

    assert capsys.readouterr().out.strip() == "event='hello'"
