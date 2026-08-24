"""Tests for the best-effort username resolution in `dltrack._identity`."""

from __future__ import annotations

import pytest

from dltrack import _identity


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_identity.ENV_VAR, raising=False)


def test_resolve_username_prefers_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_identity.ENV_VAR, "from-env")
    monkeypatch.setattr(_identity.getpass, "getuser", lambda: "from-os")

    assert _identity.resolve_username() == "from-env"


def test_resolve_username_strips_whitespace_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_identity.ENV_VAR, "  from-env  ")

    assert _identity.resolve_username() == "from-env"


def test_resolve_username_falls_back_to_os_user_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_identity.getpass, "getuser", lambda: "from-os")

    assert _identity.resolve_username() == "from-os"


def test_resolve_username_falls_back_to_os_user_when_env_var_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_identity.ENV_VAR, "   ")
    monkeypatch.setattr(_identity.getpass, "getuser", lambda: "from-os")

    assert _identity.resolve_username() == "from-os"


def test_resolve_username_falls_back_to_anonymous_when_getpass_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> str:
        msg = "no passwd entry"
        raise OSError(msg)

    monkeypatch.setattr(_identity.getpass, "getuser", _raise)

    assert _identity.resolve_username() == _identity.ANONYMOUS


def test_resolve_username_falls_back_to_anonymous_when_os_user_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_identity.getpass, "getuser", lambda: "   ")

    assert _identity.resolve_username() == _identity.ANONYMOUS
