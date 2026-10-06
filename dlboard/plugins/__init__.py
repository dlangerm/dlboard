"""All built-in dlboard plugins."""

from dlboard.models import PluginProtocol
from dlboard.plugins import auth, backend, charts, themes
from dlboard.plugins.auth import anonymous, password
from dlboard.plugins.backend import artifact_purge_worker, basic_rest_backend, error
from dlboard.plugins.data_stores import filesystem, postgres, s3, sqlite

LOCAL_STORAGE: list[PluginProtocol] = [sqlite, filesystem, artifact_purge_worker]
# Postgres metadata (configured from `POSTGRES_*` env vars -- see `postgres.PostgresSettings`) with
# the same on-disk artifact store; compose it into a `dlboard serve custom --plugins` list.
POSTGRES_STORAGE: list[PluginProtocol] = [postgres, filesystem, artifact_purge_worker]
# Postgres metadata with an S3-protocol artifact store (configured from `S3_*` env vars -- see
# `s3.S3Settings`) instead of local disk; needs the `s3` extra (`pip install 'dlboard[s3]'`).
POSTGRES_S3_STORAGE: list[PluginProtocol] = [postgres, s3, artifact_purge_worker]
LOCAL_AUTH: list[PluginProtocol] = [anonymous]
# Username/password sign-in, for hosting dlboard for other people: swap it in for `LOCAL_AUTH` in a
# `dlboard serve custom --plugins` list. Needs `DLBOARD_SECRET_KEY` -- see `password.PasswordSettings`
# and `dlboard.serve.AuthSettings`.
PASSWORD_AUTH: list[PluginProtocol] = [password]
BUILTIN_BACKEND: list[PluginProtocol] = [basic_rest_backend, error]
BUILTIN_CHARTS: list[PluginProtocol] = [
    charts.image_series,
    charts.line_chart,
    charts.bar_chart,
    charts.table_chart,
]

# Built-in pages (home, project, admin, experiment, account) aren't listed here -- their tab+accordion
# layout is opinionated, non-optional dlboard behavior, wired unconditionally by
# `dlboard.serve.app.app()` itself rather than offered as something a deployment opts into. See
# `dlboard/serve/app.py`.
LOCAL_DEPLOYMENT: list[PluginProtocol] = [
    *LOCAL_STORAGE,
    *LOCAL_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_CHARTS,
]

# What `dlboard serve local` actually runs -- `LOCAL_DEPLOYMENT` plus a theme, published here (not
# built inline in `dlboard._cli`) so `local`'s target is a real, publicly importable
# `list[PluginProtocol]`, resolved the exact same way a `dlboard serve custom --plugins ...`
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
    "auth",
    "backend",
    "charts",
    "filesystem",
    "postgres",
    "s3",
    "sqlite",
    "themes",
]
