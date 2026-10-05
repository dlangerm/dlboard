"""
`configure_logging`'s one safety property: a logged traceback must never print the locals of the
frames it passes through -- that's how a password, an API token, or (in a blob-store worker) cloud
storage credentials held by a function that then raised would end up in the log.

Every entrypoint that calls this (the CLI, `_wsgi.py`, `_blob_store.py`'s spawned writer process)
has to run it itself -- see the module docstring for why -- so this tests the shared function those
all delegate to, not any one call site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from dltrack.serve._logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def configured_logging() -> Iterator[None]:
    structlog.reset_defaults()
    configure_logging()
    yield
    structlog.reset_defaults()


def _fail_holding(password: str) -> None:
    msg = "boom"
    raise ValueError(msg)


@pytest.mark.usefixtures("configured_logging")
def test_a_logged_traceback_does_not_print_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "-".join(["hunter2", "the", "password"])
    try:
        _fail_holding(secret)
    except ValueError:
        structlog.get_logger().exception("it failed")

    output = capsys.readouterr().out
    assert "boom" in output
    assert secret not in output
