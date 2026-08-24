"""Server backend, just expose the app for running."""

from dltrack.serve._backend import _sql as sql
from dltrack.serve._backend._auth import (
    get_auth_provider,
    get_current_user,
    set_auth_provider,
    wait_for_auth_provider,
)
from dltrack.serve._backend._data_store import (
    get_artifact_store,
    get_data_store,
    set_artifact_store,
    set_data_store,
    wait_for_artifact_store,
    wait_for_data_store,
)
from dltrack.serve._backend._installed_plugins import get_installed_plugins, set_installed_plugins
from dltrack.serve.app import app

__all__ = [
    "app",
    "get_artifact_store",
    "get_auth_provider",
    "get_current_user",
    "get_data_store",
    "get_installed_plugins",
    "set_artifact_store",
    "set_auth_provider",
    "set_data_store",
    "set_installed_plugins",
    "sql",
    "wait_for_artifact_store",
    "wait_for_auth_provider",
    "wait_for_data_store",
]
