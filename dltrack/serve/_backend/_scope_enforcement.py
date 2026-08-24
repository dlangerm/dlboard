"""
Wraps a `DataStore` so its scope-gated methods can't be called without the required `Scope`.

`set_data_store` (`_data_store.py`) always wraps whatever a storage plugin registers in one of
these before attaching it to the app -- that's the one place any `DataStore` implementation ever
enters a running app, so it's also the one place enforcement can be structural instead of a
convention every backend has to remember to implement itself. `SQLStoreBase` also checks these same
scopes internally (see `_sql_store_base.py`); this wrapper doesn't replace that, it guarantees the
check happens even for a `DataStore` that doesn't -- one written from scratch against a different
backend, that forgot, or that got it wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dltrack.models import Scope, require_scope

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dltrack.models import AuditLogEntry, DataStore, User


class ScopeEnforcingDataStore:
    """
    A `DataStore` decorator that checks scopes before delegating to the wrapped store.

    Every method not listed here passes straight through to the wrapped store via `__getattr__`,
    unchanged.
    """

    def __init__(self, inner: DataStore[...]) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._inner, name)

    def delete_project(self, project_id: int, actor: User) -> None:
        require_scope(actor, Scope.PROJECT_DELETE)
        self._inner.delete_project(project_id, actor)

    def restore_project(self, project_id: int, actor: User) -> None:
        require_scope(actor, Scope.RESTORE)
        self._inner.restore_project(project_id, actor)

    def purge_project(self, project_id: int, actor: User) -> None:
        require_scope(actor, Scope.PURGE)
        self._inner.purge_project(project_id, actor)

    def delete_experiment(self, experiment_id: int, actor: User) -> None:
        require_scope(actor, Scope.EXPERIMENT_DELETE)
        self._inner.delete_experiment(experiment_id, actor)

    def restore_experiment(self, experiment_id: int, actor: User) -> None:
        require_scope(actor, Scope.RESTORE)
        self._inner.restore_experiment(experiment_id, actor)

    def purge_experiment(self, experiment_id: int, actor: User) -> None:
        require_scope(actor, Scope.PURGE)
        self._inner.purge_experiment(experiment_id, actor)

    def delete_run(self, run_id: int, actor: User) -> None:
        require_scope(actor, Scope.RUN_DELETE)
        self._inner.delete_run(run_id, actor)

    def restore_run(self, run_id: int, actor: User) -> None:
        require_scope(actor, Scope.RESTORE)
        self._inner.restore_run(run_id, actor)

    def purge_run(self, run_id: int, actor: User) -> None:
        require_scope(actor, Scope.PURGE)
        self._inner.purge_run(run_id, actor)

    def delete_artifact(self, artifact_id: int, actor: User) -> None:
        require_scope(actor, Scope.ARTIFACT_DELETE)
        self._inner.delete_artifact(artifact_id, actor)

    def restore_artifact(self, artifact_id: int, actor: User) -> None:
        require_scope(actor, Scope.RESTORE)
        self._inner.restore_artifact(artifact_id, actor)

    def purge_artifact(self, artifact_id: int, actor: User) -> None:
        require_scope(actor, Scope.PURGE)
        self._inner.purge_artifact(artifact_id, actor)

    def list_audit_log(self, actor: User, limit: int = 100, offset: int = 0) -> Iterator[AuditLogEntry]:
        require_scope(actor, Scope.AUDIT_LOG_READ)
        return self._inner.list_audit_log(actor, limit=limit, offset=offset)
