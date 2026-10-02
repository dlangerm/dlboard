"""The CLI's logging setup: a logged traceback must never print the locals of the frames it passes through."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from dltrack import _cli

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def cli_logging() -> Iterator[None]:
    structlog.reset_defaults()
    _cli._configure_logging()  # pyright: ignore[reportPrivateUsage]
    yield
    structlog.reset_defaults()


def _fail_holding(password: str) -> None:
    msg = "boom"
    raise ValueError(msg)


@pytest.mark.usefixtures("cli_logging")
def test_a_logged_traceback_does_not_print_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "-".join(["hunter2", "the", "password"])
    try:
        _fail_holding(secret)
    except ValueError:
        structlog.get_logger().exception("it failed")

    output = capsys.readouterr().out
    assert "boom" in output
    assert secret not in output
