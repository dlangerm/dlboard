"""Server backend, just expose the app for running."""

from dlboard.serve._assets import AssetKind, serve_asset
from dlboard.serve._backend import _sql as sql
from dlboard.serve._backend._api_tokens import mint_api_token
from dlboard.serve._backend._app_slot import AppSlot
from dlboard.serve._backend._auth import (
    AuthSettings,
    add_public_route,
    end_session,
    get_auth_provider,
    get_auth_settings,
    get_current_user,
    safe_next_path,
    set_auth_provider,
    sign_in,
    start_session,
)
from dlboard.serve._backend._authorization import AuthorizingDataStore
from dlboard.serve._backend._data_store import (
    get_artifact_store,
    get_data_store,
    get_project_role,
    get_system_artifact_store,
    get_system_data_store,
    set_artifact_store,
    set_data_store,
    wait_for_artifact_store,
    wait_for_data_store,
)
from dlboard.serve._backend._installed_plugins import get_installed_plugins, set_installed_plugins
from dlboard.serve._backend._sql_store_base import SCHEMA_PREP_OPTION, SQLStoreBase
from dlboard.serve._backend._startup_checks import add_startup_check
from dlboard.serve._backend._theme import get_theme, set_theme
from dlboard.serve._clientside_script import ClientsideScript
from dlboard.serve._icons import Icon, icon, icon_cell_class
from dlboard.serve._production_server import resolve_plugins, run_production_server
from dlboard.serve._series import series_color, series_swatch_class
from dlboard.serve._settings_env import set_setting_env
from dlboard.serve._url import relative_path
from dlboard.serve.app import app

__all__ = [
    "SCHEMA_PREP_OPTION",
    "AppSlot",
    "AssetKind",
    "AuthSettings",
    "AuthorizingDataStore",
    "ClientsideScript",
    "Icon",
    "SQLStoreBase",
    "add_public_route",
    "add_startup_check",
    "app",
    "end_session",
    "get_artifact_store",
    "get_auth_provider",
    "get_auth_settings",
    "get_current_user",
    "get_data_store",
    "get_installed_plugins",
    "get_project_role",
    "get_system_artifact_store",
    "get_system_data_store",
    "get_theme",
    "icon",
    "icon_cell_class",
    "mint_api_token",
    "relative_path",
    "resolve_plugins",
    "run_production_server",
    "safe_next_path",
    "series_color",
    "series_swatch_class",
    "serve_asset",
    "set_artifact_store",
    "set_auth_provider",
    "set_data_store",
    "set_installed_plugins",
    "set_setting_env",
    "set_theme",
    "sign_in",
    "sql",
    "start_session",
    "wait_for_artifact_store",
    "wait_for_data_store",
]
