"""All built-in dltrack plugins."""

from dltrack.models._plugin import PluginProtocol
from dltrack.plugins import artifacts, backend, charts, pages, themes
from dltrack.plugins.data_stores import filesystem, sqlite

LOCAL_STORAGE: list[PluginProtocol] = [sqlite, filesystem]
BUILTIN_PAGES: list[PluginProtocol] = [
    pages.simple_homepage,
    pages.simple_project_page,
    pages.simple_experiment_page,
    pages.simple_admin_page,
]
BUILTIN_CHARTS: list[PluginProtocol] = [
    charts.image_series,
    charts.line_chart,
    charts.table_chart,
]

LOCAL_DEPLOYMENT: list[PluginProtocol] = [
    *LOCAL_STORAGE,
    *BUILTIN_PAGES,
    *BUILTIN_CHARTS,
]

__all__ = [
    "BUILTIN_CHARTS",
    "BUILTIN_PAGES",
    "LOCAL_DEPLOYMENT",
    "LOCAL_STORAGE",
    "artifacts",
    "backend",
    "charts",
    "filesystem",
    "pages",
    "sqlite",
    "themes",
]
