"""The app's data store and artifact store: set once by storage plugins, read by every callback/route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from dltrack.serve._backend._app_slot import AppSlot
from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore

if TYPE_CHECKING:
    from dash import Dash

    from dltrack.models import ArtifactStore, DataStore
    from dltrack.serve._backend._sql_store_base import SQLStoreBase

DATA_STORE: AppSlot[DataStore[...]] = AppSlot("data store")
ARTIFACT_STORE: AppSlot[ArtifactStore[...]] = AppSlot("artifact store")
SQL_STORE: AppSlot[SQLStoreBase[Any]] = AppSlot("sql store")
"""
The same store as `DATA_STORE`, when it's SQL-backed -- unwrapped and with its SQL surface
(`engine`, `metadata`, `tables`, `create_tables`, `dialect`), for plugins that keep their own
tables in the same database. Set by SQL storage plugins alongside `set_data_store`.
"""

get_data_store = DATA_STORE.get
wait_for_data_store = DATA_STORE.wait
set_artifact_store = ARTIFACT_STORE.set
get_artifact_store = ARTIFACT_STORE.get
wait_for_artifact_store = ARTIFACT_STORE.wait
set_sql_store = SQL_STORE.set
get_sql_store = SQL_STORE.get


def set_data_store(app: Dash, store: DataStore[...]) -> None:
    """
    Set `app`'s data store, for storage plugins.

    Always wraps `store` in `ScopeEnforcingDataStore` first -- every `DataStore`, regardless of
    backend, gets scope enforcement whether its own implementation checks or not.
    """
    # A `__getattr__` pass-through, so it structurally satisfies `DataStore` only at runtime.
    DATA_STORE.set(app, cast("DataStore[...]", ScopeEnforcingDataStore(store)))
