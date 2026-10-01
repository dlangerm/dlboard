"""The app's data store and artifact store: set once by storage plugins, read by every callback/route."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from dltrack.serve._backend._app_slot import AppSlot
from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore

if TYPE_CHECKING:
    from dash import Dash

    from dltrack.models import ArtifactStore, DataStore

DATA_STORE: AppSlot[DataStore[...]] = AppSlot("data store")
ARTIFACT_STORE: AppSlot[ArtifactStore[...]] = AppSlot("artifact store")

get_data_store = DATA_STORE.get
# For work no user is behind -- the request gate resolving who a caller is, background workers.
get_system_data_store = DATA_STORE.get
wait_for_data_store = DATA_STORE.wait
set_artifact_store = ARTIFACT_STORE.set
get_artifact_store = ARTIFACT_STORE.get
get_system_artifact_store = ARTIFACT_STORE.get
wait_for_artifact_store = ARTIFACT_STORE.wait


def set_data_store(app: Dash, store: DataStore[...]) -> None:
    """
    Set `app`'s data store, for storage plugins.

    Always wraps `store` in `ScopeEnforcingDataStore` first -- every `DataStore`, regardless of
    backend, gets scope enforcement whether its own implementation checks or not.
    """
    # A `__getattr__` pass-through, so it structurally satisfies `DataStore` only at runtime.
    DATA_STORE.set(app, cast("DataStore[...]", ScopeEnforcingDataStore(store)))
