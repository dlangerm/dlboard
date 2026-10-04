"""
The plugin list the Docker image runs by default -- Postgres + S3 storage, password sign-in.

dltrack ships ready-made storage bundles (`dltrack.plugins.POSTGRES_S3_STORAGE`) but deliberately
doesn't ship a full "production deployment" list of its own: a deployment composes its own
`PLUGINS` and runs it with `dltrack serve custom --plugins yourmodule:PLUGINS` (see
docs/plugins/storage.md). The Docker image needs a real, importable target the same way, so this
module is that -- not a general-purpose library export, just the one composition the image's own
`CMD` points at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

if TYPE_CHECKING:
    from dltrack.models import PluginProtocol

PLUGINS: list[PluginProtocol] = [
    *POSTGRES_S3_STORAGE,
    *PASSWORD_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_CHARTS,
    themes.default,
]
