"""Common backend functions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack._models.settings import AppSettings
from dltrack._server.backend.stores import sqlite_store

if TYPE_CHECKING:
    from dltrack._models.data_store import DataStore


def get_store() -> DataStore:
    settings = AppSettings()
    match settings.store_type:
        case "sqllite":
            assert settings.sqllite_location is not None, "sqllite location must be given."
            return sqlite_store.SQLLiteStore.get_or_create(loc=settings.sqllite_location)
    raise NotImplementedError(settings.store_type)
