"""All built-in dltrack plugins."""

from dltrack.models import PluginProtocol
from dltrack.plugins import artifacts, auth, backend, charts, themes
from dltrack.plugins.auth import anonymous, password
from dltrack.plugins.backend import artifact_purge_worker, basic_rest_backend, error
from dltrack.plugins.data_stores import filesystem, postgres, s3, sqlite

LOCAL_STORAGE: list[PluginProtocol] = [sqlite, filesystem, artifact_purge_worker]
# Postgres metadata (configured from `POSTGRES_*` env vars -- see `postgres.PostgresSettings`) with
# the same on-disk artifact store; compose it into a `dltrack serve custom --plugins` list.
POSTGRES_STORAGE: list[PluginProtocol] = [postgres, filesystem, artifact_purge_worker]
# Postgres metadata with an S3-protocol artifact store (configured from `S3_*` env vars -- see
# `s3.S3Settings`) instead of local disk; needs the `s3` extra (`pip install 'dltrack[s3]'`).
POSTGRES_S3_STORAGE: list[PluginProtocol] = [postgres, s3, artifact_purge_worker]
LOCAL_AUTH: list[PluginProtocol] = [anonymous]
# Username/password sign-in, for hosting dltrack for other people: swap it in for `LOCAL_AUTH` in a
# `dltrack serve custom --plugins` list. Needs `DLTRACK_SECRET_KEY` -- see `password.PasswordSettings`
# and `dltrack.serve.AuthSettings`.
PASSWORD_AUTH: list[PluginProtocol] = [password]
BUILTIN_BACKEND: list[PluginProtocol] = [basic_rest_backend, error]
BUILTIN_CHARTS: list[PluginProtocol] = [
    charts.image_series,
    charts.line_chart,
    charts.bar_chart,
    charts.table_chart,
]

# Built-in pages (home, project, admin, experiment) aren't listed here -- their tab+accordion
# layout is opinionated, non-optional dltrack behavior, wired unconditionally by
# `dltrack.serve.app.app()` itself rather than offered as something a deployment opts into. See
# `dltrack/serve/app.py`.
LOCAL_DEPLOYMENT: list[PluginProtocol] = [
    *LOCAL_STORAGE,
    *LOCAL_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_CHARTS,
]

# What `dltrack serve local` actually runs -- `LOCAL_DEPLOYMENT` plus a theme, published here (not
# built inline in `dltrack._cli`) so `local`'s target is a real, publicly importable
# `list[PluginProtocol]`, resolved the exact same way a `dltrack serve custom --plugins ...`
# deployment resolves its own.
LOCAL_DEPLOYMENT_DEFAULT: list[PluginProtocol] = [*LOCAL_DEPLOYMENT, themes.default]

__all__ = [
    "BUILTIN_BACKEND",
    "BUILTIN_CHARTS",
    "LOCAL_AUTH",
    "LOCAL_DEPLOYMENT",
    "LOCAL_DEPLOYMENT_DEFAULT",
    "LOCAL_STORAGE",
    "PASSWORD_AUTH",
    "POSTGRES_S3_STORAGE",
    "POSTGRES_STORAGE",
    "artifacts",
    "auth",
    "backend",
    "charts",
    "filesystem",
    "postgres",
    "s3",
    "sqlite",
    "themes",
]
