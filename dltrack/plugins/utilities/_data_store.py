"""A plugin using dash hooks that exposes the current data store to callbacks via callback context."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Final, cast

from dash import Dash, get_app

if TYPE_CHECKING:
    from dltrack.models import DataStore

_DLTRACK_STORE_ATTRIBUTE: Final = "_dltrack_data_store_"


def set_data_store(app: Dash, store: DataStore[...]) -> None:
    """Set the data store for a dash app, use this in plugins."""
    if hasattr(app, _DLTRACK_STORE_ATTRIBUTE):
        msg = "Refusing to overwrite an existing set data store."
        raise AttributeError(msg)
    setattr(app, _DLTRACK_STORE_ATTRIBUTE, store)


@cache
def get_data_store() -> DataStore[...]:
    """Strongly typed helper function for callbacks who need the store."""
    app = cast("Dash", get_app())
    if not hasattr(app, _DLTRACK_STORE_ATTRIBUTE):
        msg = "Data store was not set for the app"
        raise AttributeError(msg)
    return cast("DataStore[...]", getattr(app, _DLTRACK_STORE_ATTRIBUTE))
