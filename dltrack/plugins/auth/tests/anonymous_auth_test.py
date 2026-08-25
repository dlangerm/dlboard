"""Tests for `dltrack.plugins.auth.anonymous`: the default zero-friction auth provider."""

from __future__ import annotations

import pytest
from flask import Flask

from dltrack.plugins.auth import anonymous

_app = Flask(__name__)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLTRACK_USER", raising=False)


def test_resolve_identity_prefers_the_request_header() -> None:
    provider = anonymous.AnonymousAuthProvider.get_or_create()
    with _app.test_request_context(headers={anonymous.DLTRACK_USER_HEADER: "alice"}):
        assert provider.resolve_identity() == "alice"


def test_resolve_identity_falls_back_to_environment_when_header_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "resolve_username", lambda: "from-env")
    provider = anonymous.AnonymousAuthProvider.get_or_create()
    with _app.test_request_context(headers={anonymous.DLTRACK_USER_HEADER: "   "}):
        assert provider.resolve_identity() == "from-env"


def test_resolve_identity_falls_back_to_environment_outside_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "resolve_username", lambda: "from-env")
    provider = anonymous.AnonymousAuthProvider.get_or_create()

    assert provider.resolve_identity() == "from-env"
