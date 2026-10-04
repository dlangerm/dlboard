"""
The plugin list the Docker image runs by default -- Postgres + local-disk artifacts, password sign-in.

dltrack ships ready-made storage bundles (`dltrack.plugins.POSTGRES_STORAGE`) but deliberately
doesn't ship a full "production deployment" list of its own: a deployment composes its own
`PLUGINS` and runs it with `dltrack serve custom --plugins yourmodule:PLUGINS` (see
docs/plugins/storage.md). The Docker image needs a real, importable target the same way, so this
module is that -- not a general-purpose library export, just the one composition the image's own
`CMD` points at.

Local disk (not S3) for artifacts, specifically -- the image ships the `s3` extra too, so swapping
in `dltrack.plugins.POSTGRES_S3_STORAGE` needs no rebuild, just your own module pointed at by
`--plugins` (see docs/docker.md's "Using an S3 artifact store instead").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_STORAGE, themes

if TYPE_CHECKING:
    from dltrack.models import PluginProtocol

PLUGINS: list[PluginProtocol] = [
    *POSTGRES_STORAGE,
    *PASSWORD_AUTH,
    *BUILTIN_BACKEND,
    *BUILTIN_CHARTS,
    themes.default,
]
