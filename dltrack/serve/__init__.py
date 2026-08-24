"""Server backend, just expose the app for running."""

from dltrack.serve._backend import _sql as sql
from dltrack.serve._backend._data_store import (
    get_artifact_store,
    get_data_store,
    set_artifact_store,
    set_data_store,
)
from dltrack.serve.app import app

__all__ = ["app", "get_artifact_store", "get_data_store", "set_artifact_store", "set_data_store", "sql"]
