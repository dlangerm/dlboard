"""A plugin using dash hooks that exposes the current data store to callbacks via callback context."""

from __future__ import annotations

import time
from functools import cache
from typing import TYPE_CHECKING, Final, cast

from dash import Dash, get_app
from dash.exceptions import AppNotFoundError
from structlog.stdlib import get_logger

from dltrack.serve._backend._scope_enforcement import ScopeEnforcingDataStore

_log = get_logger(__name__)


if TYPE_CHECKING:
    from dltrack.models import ArtifactStore, DataStore

_DLTRACK_STORE_ATTRIBUTE: Final = "_dltrack_data_store_"
_DLTRACK_ARTIFACT_STORE: Final = "_dltrack_artifact_store_"


def set_data_store(app: Dash, store: DataStore[...]) -> None:
    """
    Set the data store for a dash app, use this in plugins.

    Always wraps `store` in `ScopeEnforcingDataStore` first -- every `DataStore`, regardless of
    backend, gets scope enforcement whether its own implementation checks or not.
    """
    if hasattr(app, _DLTRACK_STORE_ATTRIBUTE):
        msg = "Refusing to overwrite an existing set data store."
        raise AttributeError(msg)
    _log.info("set data store to %s", store.__class__.__name__)
    setattr(app, _DLTRACK_STORE_ATTRIBUTE, ScopeEnforcingDataStore(store))


def set_artifact_store(app: Dash, store: ArtifactStore[...]) -> None:
    """Set the artifact store for the dash app."""
    if hasattr(app, _DLTRACK_ARTIFACT_STORE):
        msg = "Refusing to overwrite an existing set artifact store."
        raise AttributeError(msg)
    _log.info("set artifact store to %s", store.__class__.__name__)
    setattr(app, _DLTRACK_ARTIFACT_STORE, store)


@cache
def get_data_store() -> DataStore[...]:
    """Strongly typed helper function for callbacks who need the store."""
    app = cast("Dash", get_app())
    if not hasattr(app, _DLTRACK_STORE_ATTRIBUTE):
        msg = "Data store was not set for the app"
        raise AttributeError(msg)
    return cast("DataStore[...]", getattr(app, _DLTRACK_STORE_ATTRIBUTE))


@cache
def get_artifact_store() -> ArtifactStore[...]:
    """Get the artifact store for the app."""
    app = cast("Dash", get_app())
    if not hasattr(app, _DLTRACK_ARTIFACT_STORE):
        msg = "Data store was not set for the app"
        raise AttributeError(msg)
    return cast("ArtifactStore[...]", getattr(app, _DLTRACK_ARTIFACT_STORE))


def wait_for_data_store() -> DataStore[...]:
    """
    Like `get_data_store`, but retries once a second until the app exists.

    For a plugin whose own `plug()` runs before another plugin has called `set_data_store` yet, or
    whose background thread starts before `Dash.__init__` (which runs every plugin's `plug()`) has
    even finished -- plugin registration order isn't guaranteed.
    """
    try:
        return get_data_store()
    except AppNotFoundError:
        time.sleep(1)
        return wait_for_data_store()


def wait_for_artifact_store() -> ArtifactStore[...]:
    """Like `get_artifact_store`, but retries once a second until the app exists. See `wait_for_data_store`."""
    try:
        return get_artifact_store()
    except AppNotFoundError:
        time.sleep(1)
        return wait_for_artifact_store()
