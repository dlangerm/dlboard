"""Tests for `dlboard.plugins.auth.anonymous`: the default zero-friction auth provider."""

from __future__ import annotations

import pytest
from flask import Flask

from dlboard import models
from dlboard.plugins.auth import anonymous

_app = Flask(__name__)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLBOARD_USER", raising=False)


def test_authenticate_prefers_the_request_header() -> None:
    provider = anonymous.AnonymousAuthProvider.get_or_create()
    with _app.test_request_context(headers={anonymous.DLBOARD_USER_HEADER: "alice"}):
        assert provider.authenticate() == models.Principal.unverified("alice")


def test_authenticate_falls_back_to_environment_when_header_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "resolve_username", lambda: "from-env")
    provider = anonymous.AnonymousAuthProvider.get_or_create()
    with _app.test_request_context(headers={anonymous.DLBOARD_USER_HEADER: "   "}):
        assert provider.authenticate() == models.Principal.unverified("from-env")


def test_authenticate_falls_back_to_environment_outside_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anonymous, "resolve_username", lambda: "from-env")
    provider = anonymous.AnonymousAuthProvider.get_or_create()

    assert provider.authenticate() == models.Principal.unverified("from-env")
