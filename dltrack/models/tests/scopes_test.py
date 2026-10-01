"""Tests for `dltrack.models.Scope`/`has_scope`."""

from __future__ import annotations

from datetime import UTC, datetime

from dltrack import models


def _user(scopes: list[models.Scope]) -> models.User:
    return models.User(
        id=1,
        username="u",
        issuer="test",
        subject="u",
        scopes=scopes,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_has_scope_true_for_directly_granted_scope() -> None:
    user = _user([models.Scope.PURGE])
    assert models.has_scope(user, models.Scope.PURGE)


def test_has_scope_false_for_ungranted_scope() -> None:
    user = _user([models.Scope.RESTORE])
    assert not models.has_scope(user, models.Scope.PURGE)


def test_has_scope_true_via_wildcard() -> None:
    user = _user([models.Scope.ALL])
    assert models.has_scope(user, models.Scope.PURGE)
    assert models.has_scope(user, models.Scope.AUDIT_LOG_READ)


def test_has_scope_false_for_user_with_no_scopes() -> None:
    user = _user([])
    assert not models.has_scope(user, models.Scope.PURGE)
