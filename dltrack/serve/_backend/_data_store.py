"""A plugin using dash hooks that exposes the current data store to callbacks via callback context."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Final, cast

from dash import Dash, get_app
from structlog.stdlib import get_logger

_log = get_logger(__name__)


if TYPE_CHECKING:
    from dltrack.models import ArtifactStore, DataStore

_DLTRACK_STORE_ATTRIBUTE: Final = "_dltrack_data_store_"
_DLTRACK_ARTIFACT_STORE: Final = "_dltrack_artifact_store_"


def set_data_store(app: Dash, store: DataStore[...]) -> None:
    """Set the data store for a dash app, use this in plugins."""
    if hasattr(app, _DLTRACK_STORE_ATTRIBUTE):
        msg = "Refusing to overwrite an existing set data store."
        raise AttributeError(msg)
    _log.info("set data store to %s", store.__class__.__name__)
    setattr(app, _DLTRACK_STORE_ATTRIBUTE, store)


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
