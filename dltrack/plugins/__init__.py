"""All built-in dltrack plugins."""

from dltrack.models import PluginProtocol
from dltrack.plugins import artifacts, auth, backend, charts, pages, themes
from dltrack.plugins.auth import anonymous
from dltrack.plugins.backend import artifact_purge_worker, basic_rest_backend, error
from dltrack.plugins.data_stores import filesystem, sqlite

LOCAL_STORAGE: list[PluginProtocol] = [sqlite, filesystem, artifact_purge_worker]
LOCAL_AUTH: list[PluginProtocol] = [anonymous]
BUILTIN_BACKEND: list[PluginProtocol] = [basic_rest_backend, error]
BUILTIN_PAGES: list[PluginProtocol] = [
    pages.simple_homepage,
    pages.simple_project_page,
    pages.simple_experiment_page,
    pages.simple_admin_page,
]
BUILTIN_CHARTS: list[PluginProtocol] = [
    charts.image_series,
    charts.line_chart,
    charts.bar_chart,
    charts.table_chart,
]

LOCAL_DEPLOYMENT: list[PluginProtocol] = [
    *LOCAL_STORAGE,
    *LOCAL_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_PAGES,
    *BUILTIN_CHARTS,
]

__all__ = [
    "BUILTIN_BACKEND",
    "BUILTIN_CHARTS",
    "BUILTIN_PAGES",
    "LOCAL_AUTH",
    "LOCAL_DEPLOYMENT",
    "LOCAL_STORAGE",
    "artifacts",
    "auth",
    "backend",
    "charts",
    "filesystem",
    "pages",
    "sqlite",
    "themes",
]
