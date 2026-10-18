"""A server that can't start says why, once, as a structured error on the span that failed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry.trace import StatusCode
from structlog.testing import capture_logs

from dlboard._telemetry.traces import flush
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from dash import Dash
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class _Broken:
    @classmethod
    def plug(cls, app: Dash) -> None:
        msg = "the database is gone"
        raise ConnectionError(msg)


def test_a_plugin_that_fails_to_plug_is_logged_once_as_an_error_and_fails_the_startup_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    with capture_logs() as logs, pytest.raises(ConnectionError, match="the database is gone"):
        build_app([_Broken])
    flush()

    (failure,) = [event for event in logs if event["log_level"] == "error"]
    assert failure["event"] == "dlboard could not start"
    assert failure["exc_info"]
    startup = next(s for s in span_exporter.get_finished_spans() if s.name == "dlboard.startup")
    assert startup.status.status_code is StatusCode.ERROR
