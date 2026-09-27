"""Server backend, just expose the app for running."""

from dltrack.serve._assets import AssetKind, serve_asset
from dltrack.serve._backend import _sql as sql
from dltrack.serve._backend._auth import get_auth_provider, get_current_user, set_auth_provider
from dltrack.serve._backend._data_store import (
    get_artifact_store,
    get_data_store,
    set_artifact_store,
    set_data_store,
    wait_for_artifact_store,
    wait_for_data_store,
)
from dltrack.serve._backend._installed_plugins import get_installed_plugins, set_installed_plugins
from dltrack.serve._backend._sql_store_base import SQLStoreBase
from dltrack.serve._backend._theme import get_theme, set_theme
from dltrack.serve._clientside_script import ClientsideScript
from dltrack.serve._icons import Icon, icon, icon_cell_class
from dltrack.serve._production_server import resolve_plugins, run_production_server
from dltrack.serve._series import series_color
from dltrack.serve._settings_env import set_setting_env
from dltrack.serve.app import app

__all__ = [
    "AssetKind",
    "ClientsideScript",
    "Icon",
    "SQLStoreBase",
    "app",
    "get_artifact_store",
    "get_auth_provider",
    "get_current_user",
    "get_data_store",
    "get_installed_plugins",
    "get_theme",
    "icon",
    "icon_cell_class",
    "resolve_plugins",
    "run_production_server",
    "series_color",
    "serve_asset",
    "set_artifact_store",
    "set_auth_provider",
    "set_data_store",
    "set_installed_plugins",
    "set_setting_env",
    "set_theme",
    "sql",
    "wait_for_artifact_store",
    "wait_for_data_store",
]
