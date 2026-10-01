"""
Tests for `ScopeEnforcingDataStore`: the wrapper `set_data_store` puts around every `DataStore`.

The point of this wrapper is that it enforces scopes even for a backend that doesn't check them
itself -- so `_UnscopedFakeStore` below deliberately does *no* scope checking of its own, unlike
`SQLStoreBase`. If these tests pass against it, a noncompliant `DataStore` plugin can't bypass
enforcement by simply not calling `require_scope`.
"""

from __future__ import annotations

import pendulum
import pytest

from dltrack import models
from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore


class _UnscopedFakeStore:
    """A minimal `DataStore` stand-in whose scope-gated methods perform no scope check at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_project(self, project_id: int, actor: models.User) -> None:
        self.calls.append("delete_project")

    def restore_project(self, project_id: int, actor: models.User) -> None:
        self.calls.append("restore_project")

    def purge_project(self, project_id: int, actor: models.User) -> None:
        self.calls.append("purge_project")

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        self.calls.append("delete_experiment")

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        self.calls.append("restore_experiment")

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        self.calls.append("purge_experiment")

    def delete_run(self, run_id: int, actor: models.User) -> None:
        self.calls.append("delete_run")

    def restore_run(self, run_id: int, actor: models.User) -> None:
        self.calls.append("restore_run")

    def purge_run(self, run_id: int, actor: models.User) -> None:
        self.calls.append("purge_run")

    def delete_artifact(self, artifact_id: int, actor: models.User) -> None:
        self.calls.append("delete_artifact")

    def restore_artifact(self, artifact_id: int, actor: models.User) -> None:
        self.calls.append("restore_artifact")

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        self.calls.append("purge_artifact")

    def list_audit_log(
        self, actor: models.User, limit: int = 100, offset: int = 0
    ) -> "list[models.AuditLogEntry]":
        self.calls.append("list_audit_log")
        return []

    def get_project(self, database_id: int) -> str:
        """A non-scoped method, to prove `__getattr__` passthrough still works."""
        self.calls.append("get_project")
        return "not-scope-gated"


def _user(*scopes: models.Scope) -> models.User:
    return models.User(
        id=1,
        username="u",
        issuer="test",
        subject="u",
        scopes=list(scopes),
        created_at=pendulum.now(pendulum.UTC),
    )


def _wrap(inner: _UnscopedFakeStore) -> ScopeEnforcingDataStore:
    """`_UnscopedFakeStore` deliberately only implements the methods these tests exercise, not the
    full `DataStore` protocol -- that's the point (see the module docstring), so this is the one
    place that needs to wave off the resulting type mismatch."""
    return ScopeEnforcingDataStore(inner)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("method_name", "scope"),
    [
        ("delete_project", models.Scope.PROJECT_DELETE),
        ("restore_project", models.Scope.RESTORE),
        ("purge_project", models.Scope.PURGE),
        ("delete_experiment", models.Scope.EXPERIMENT_DELETE),
        ("restore_experiment", models.Scope.RESTORE),
        ("purge_experiment", models.Scope.PURGE),
        ("delete_run", models.Scope.RUN_DELETE),
        ("restore_run", models.Scope.RESTORE),
        ("purge_run", models.Scope.PURGE),
        ("delete_artifact", models.Scope.ARTIFACT_DELETE),
        ("restore_artifact", models.Scope.RESTORE),
        ("purge_artifact", models.Scope.PURGE),
    ],
)
def test_wrapper_blocks_an_unscoped_backend_without_the_required_scope(
    method_name: str, scope: models.Scope
) -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)

    with pytest.raises(PermissionError, match="lacks"):
        getattr(wrapped, method_name)(1, _user())

    assert inner.calls == [], "the inner (noncompliant) store must never be reached without the scope"


@pytest.mark.parametrize(
    ("method_name", "scope"),
    [
        ("delete_project", models.Scope.PROJECT_DELETE),
        ("restore_project", models.Scope.RESTORE),
        ("purge_project", models.Scope.PURGE),
        ("delete_experiment", models.Scope.EXPERIMENT_DELETE),
        ("restore_experiment", models.Scope.RESTORE),
        ("purge_experiment", models.Scope.PURGE),
        ("delete_run", models.Scope.RUN_DELETE),
        ("restore_run", models.Scope.RESTORE),
        ("purge_run", models.Scope.PURGE),
        ("delete_artifact", models.Scope.ARTIFACT_DELETE),
        ("restore_artifact", models.Scope.RESTORE),
        ("purge_artifact", models.Scope.PURGE),
    ],
)
def test_wrapper_delegates_once_the_scope_is_present(method_name: str, scope: models.Scope) -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)

    getattr(wrapped, method_name)(1, _user(scope))

    assert inner.calls == [method_name]


def test_wrapper_blocks_list_audit_log_without_the_scope() -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)

    with pytest.raises(PermissionError, match="lacks"):
        wrapped.list_audit_log(_user())

    assert inner.calls == []


def test_wrapper_delegates_list_audit_log_once_the_scope_is_present() -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)

    wrapped.list_audit_log(_user(models.Scope.AUDIT_LOG_READ))

    assert inner.calls == ["list_audit_log"]


def test_wrapper_still_forwards_unscoped_methods_via_getattr() -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)

    assert wrapped.get_project(1) == "not-scope-gated"
    assert inner.calls == ["get_project"]


def test_backend_name_reports_the_wrapped_stores_class_name() -> None:
    wrapped = _wrap(_UnscopedFakeStore())

    assert wrapped.backend_name == "_UnscopedFakeStore"


def test_scope_all_bypasses_every_gate() -> None:
    inner = _UnscopedFakeStore()
    wrapped = _wrap(inner)
    admin = _user(models.Scope.ALL)

    wrapped.delete_project(1, admin)
    wrapped.purge_artifact(1, admin)
    wrapped.list_audit_log(admin)

    assert inner.calls == ["delete_project", "purge_artifact", "list_audit_log"]
