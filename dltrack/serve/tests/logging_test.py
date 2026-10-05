"""
`configure_logging`'s behavior: `DLTRACK_LOG_LEVEL`/`DLTRACK_LOG_FORMAT`, and its one safety
property -- a logged traceback must never print the locals of the frames it passes through, in
either format. That's how a password, an API token, or (in a blob-store worker) cloud storage
credentials held by a function that then raised would end up in the log.

Every entrypoint that calls this (the CLI, `_wsgi.py`, `_blob_store.py`'s spawned writer process)
has to run it itself -- see the module docstring for why -- so this tests the shared function those
all delegate to, not any one call site.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import structlog

from dltrack.serve._logging import LogFormat, configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def _reset_structlog() -> Iterator[None]:
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def _fail_holding(password: str) -> None:
    msg = "boom"
    raise ValueError(msg)


@pytest.mark.usefixtures("_reset_structlog")
@pytest.mark.parametrize("log_format", list(LogFormat))
def test_a_logged_traceback_does_not_print_local_variables(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, log_format: LogFormat
) -> None:
    monkeypatch.setenv("DLTRACK_LOG_FORMAT", log_format.value)
    configure_logging()
    secret = "-".join(["hunter2", "the", "password"])
    try:
        _fail_holding(secret)
    except ValueError:
        structlog.get_logger().exception("it failed")

    output = capsys.readouterr().out
    assert "boom" in output
    assert secret not in output


@pytest.mark.usefixtures("_reset_structlog")
def test_log_level_filters_out_quieter_messages(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLTRACK_LOG_LEVEL", "WARNING")
    configure_logging()

    structlog.get_logger().info("should not appear")
    structlog.get_logger().warning("should appear")

    output = capsys.readouterr().out
    assert "should not appear" not in output
    assert "should appear" in output


@pytest.mark.usefixtures("_reset_structlog")
def test_json_format_produces_one_parseable_json_object_per_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLTRACK_LOG_FORMAT", "json")
    configure_logging()

    structlog.get_logger().info("hello", run_id=42)

    line = capsys.readouterr().out.strip()
    parsed = json.loads(line)
    assert parsed["event"] == "hello"
    assert parsed["run_id"] == 42
