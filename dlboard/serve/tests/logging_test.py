"""
`configure_logging`'s behavior: `DLBOARD_LOG_LEVEL`/`DLBOARD_LOG_FORMAT`, and its one safety
property -- a logged traceback must never print the locals of the frames it passes through, in
either format. That's how a password, an API token, or (in a blob-store worker) cloud storage
credentials held by a function that then raised would end up in the log.

Every entrypoint that calls this (the CLI, `_wsgi.py`, `_blob_store.py`'s spawned writer process)
has to run it itself -- see the module docstring for why -- so this tests the shared function those
all delegate to, not any one call site.
"""

from __future__ import annotations

import json
import logging
import warnings
from typing import TYPE_CHECKING

import pytest
import structlog

from dlboard.serve._logging import LogFormat, configure_logging

if TYPE_CHECKING:
    from collections.abc import Iterator


_LOGGERS = ("", "dlboard", "_granian")
"""The standard-library loggers `logging_config` sets up (the root one is named `""`)."""


@pytest.fixture
def _reset_structlog() -> Iterator[None]:
    """A test starts with neither structlog nor the standard library configured, and leaves both that way."""
    saved = {
        name: (logger.handlers[:], logger.level, logger.propagate)
        for name in _LOGGERS
        for logger in [logging.getLogger(name)]
    }
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()
    for name, (handlers, level, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _fail_holding(password: str) -> None:
    msg = "boom"
    raise ValueError(msg)


@pytest.mark.usefixtures("_reset_structlog")
@pytest.mark.parametrize("log_format", list(LogFormat))
def test_a_logged_traceback_does_not_print_local_variables(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, log_format: LogFormat
) -> None:
    monkeypatch.setenv("DLBOARD_LOG_FORMAT", log_format.value)
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
    monkeypatch.setenv("DLBOARD_LOG_LEVEL", "WARNING")
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
    monkeypatch.setenv("DLBOARD_LOG_FORMAT", "json")
    configure_logging()

    structlog.get_logger().info("hello", run_id=42)

    line = capsys.readouterr().out.strip()
    parsed = json.loads(line)
    assert parsed["event"] == "hello"
    assert parsed["run_id"] == 42


@pytest.mark.usefixtures("_reset_structlog")
@pytest.mark.parametrize(
    ("logger_name", "level", "shown"),
    [
        pytest.param("flask.app", logging.ERROR, True, id="flask-exception"),
        pytest.param("_granian", logging.INFO, True, id="granian-itself"),
        pytest.param("dlboard.client._batching", logging.INFO, True, id="a-dlboard-logger"),
        pytest.param("dlboard.client._batching", logging.DEBUG, False, id="below-the-configured-level"),
        pytest.param("urllib3.connectionpool", logging.INFO, False, id="a-library-below-warning"),
    ],
)
def test_standard_library_records_are_rendered_like_our_own_events(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    logger_name: str,
    level: int,
    *,
    shown: bool,
) -> None:
    monkeypatch.setenv("DLBOARD_LOG_FORMAT", "json")
    configure_logging()

    logging.getLogger(logger_name).log(level, "from a library")

    output = capsys.readouterr().out.strip()
    if shown:
        parsed = json.loads(output)
        assert parsed["event"] == "from a library"
        assert parsed["logger"] == logger_name
        assert parsed["level"] == logging.getLevelName(level).lower()
    else:
        assert output == ""


@pytest.mark.usefixtures("_reset_structlog")
def test_configuring_twice_is_silent_and_changes_nothing() -> None:
    configure_logging()
    processors = structlog.get_config()["processors"]

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        configure_logging()  # a forked Granian worker, which inherits the parent's setup

    assert structlog.get_config()["processors"] is processors


@pytest.mark.usefixtures("_reset_structlog")
def test_a_library_that_sets_its_own_level_still_speaks_only_from_warning(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_LOG_FORMAT", "json")
    configure_logging()
    werkzeug = logging.getLogger("werkzeug")
    werkzeug.setLevel(logging.INFO)  # as Werkzeug's own server does the first time it logs
    try:
        werkzeug.info("a request")
        werkzeug.warning("a problem")
    finally:
        werkzeug.setLevel(logging.NOTSET)

    (line,) = capsys.readouterr().out.strip().splitlines()
    assert json.loads(line)["event"] == "a problem"
