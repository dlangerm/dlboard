"""
The client's requests as traces, against a live backend and the real SDK: the server's span continues the client's.

Everything runs in one process here (the shipping processes are covered where a spawned process matters, in
`dlboard/tests/traces_e2e_test.py`), so one in-memory exporter sees both ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import requests
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr

from dlboard._telemetry.traces import TraceKey, flush
from dlboard.client._rest_api import BasicDlboardAPI

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from dlboard.conftest import BackendServer


def test_a_request_is_one_trace_across_the_client_and_the_server(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    BasicDlboardAPI(backend_server.url).get_or_create_project("traced")
    flush()

    spans = span_exporter.get_finished_spans()
    (client,) = (s for s in spans if s.kind == SpanKind.CLIENT and s.name.startswith("POST"))
    (server,) = (s for s in spans if s.kind == SpanKind.SERVER)
    assert client.context is not None
    assert server.context is not None
    assert server.context.trace_id == client.context.trace_id
    assert server.parent is not None
    assert server.parent.span_id == client.context.span_id
    assert client.name == "POST /api/v1/projects/get-or-create"
    assert client.attributes is not None
    assert server.attributes is not None
    assert client.attributes[TraceKey.REQUEST_ID] == server.attributes[TraceKey.REQUEST_ID]


def test_a_rejected_request_is_traced_on_both_sides_and_never_holds_the_credential(
    span_exporter: InMemorySpanExporter, backend_server: BackendServer
) -> None:
    """The anonymous test server turns away any API key, so this request is refused with a 401."""
    api = BasicDlboardAPI(backend_server.url, api_key=SecretStr("dlb_a-credential-no-span-may-hold"))

    with pytest.raises(requests.HTTPError):
        api.get_or_create_project("rejected")
    flush()

    spans = span_exporter.get_finished_spans()
    (client,) = (s for s in spans if s.kind == SpanKind.CLIENT and s.name.startswith("POST"))
    assert client.status.status_code == StatusCode.ERROR
    assert [s for s in spans if s.kind == SpanKind.SERVER], (
        "the auth gate turns the request away, but the server still traces it"
    )
    assert all("dlb_a-credential" not in f"{s.attributes} {s.events}" for s in spans)


def test_a_request_with_tracing_off_still_has_its_own_request_id(
    backend_server: BackendServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = BasicDlboardAPI(backend_server.url)
    sent: list[dict[str, str]] = []
    send = api._session.request

    def spy(method: str, url: str, **kwargs: Any) -> requests.Response:  # noqa: ANN401
        sent.append(kwargs["headers"])
        return send(method, url, **kwargs)

    monkeypatch.setattr(api._session, "request", spy)

    api.get_or_create_project("untraced")

    assert all("traceparent" not in h for h in sent)
    assert all(h["X-Request-Id"] for h in sent)
