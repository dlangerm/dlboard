"""
Serve an artifact's blob by its database id, e.g. `/artifact/42`.

A chart that shows an artifact (the image-series chart, say) only ever needs a URL a browser can
GET -- never the artifact's `ref` (a backend-specific blob location: `file:///...`, `s3://...`)
directly. Routing by id here, rather than by ref, keeps that ref entirely server-side: it goes
through the same soft-delete/scope checks as every other artifact read, and no chart plugin ever
has to know or care which `ArtifactStore` backend is in play.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from flask import Response, abort
from pydantic import AnyUrl
from structlog.stdlib import get_logger
from werkzeug.utils import secure_filename

from dlboard.models import INLINEABLE_ARTIFACT_CONTENT_TYPES
from dlboard.serve._backend._data_store import get_artifact_store, get_data_store
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash import Dash

    from dlboard.models import Artifact

_log = get_logger(__name__)

_IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
"""What an artifact URL's bytes may be cached as: forever, since `artifact_url` puts a version in the URL that changes
whenever the artifact behind an id does. Only by the browser (`private`): who may see it is per-user, so a shared
proxy cache must never keep a copy."""


def artifact_version(artifact: Artifact) -> str:
    """
    A token that differs between two artifacts that share an id: where the blob lives, and when it was logged.

    The route is by id, but an id is only unique within one database: a browser that cached
    `/artifact/5` from an earlier database (wiped, restored, or a different `--sqlite-location`) on
    the same address would otherwise show that image for as long as `_IMMUTABLE_CACHE_CONTROL` says --
    a year -- in place of this database's artifact 5.
    """
    return hashlib.sha256(f"{artifact.ref}|{artifact.created_at.isoformat()}".encode()).hexdigest()[:12]


def artifact_url(artifact: Artifact) -> str:
    """
    The URL a browser (or a chart's own `<img>`/`<a>`) fetches `artifact` from.

    `v` is `artifact_version`, so a cached copy is only ever reused for the artifact it was fetched
    for. The route ignores it.
    """
    return relative_path(f"/artifact/{artifact.id}?v={artifact_version(artifact)}")


def _download(artifact_id: int) -> Response:
    """Look up `artifact_id`, then stream its blob. 404s for a missing or soft-deleted artifact."""
    artifact = get_data_store().get_artifact(artifact_id)
    if artifact is None:
        _log.warning("Artifact %s not found (missing or deleted)", artifact_id)
        abort(404)
    response = get_artifact_store().download_artifact(AnyUrl(artifact.ref))
    # A redirect to a short-lived presigned URL (`S3Blobs.download`'s presign mode) carries none of
    # the actual blob's bytes -- the browser fetches those straight from S3, under headers `S3Blobs`
    # sets on the presigned URL itself, not here. Cache-Control and the content-sniffing/disposition
    # headers below only apply to a response this server actually serves the bytes on (`PROXY` mode,
    # or the filesystem backend).
    if not (300 <= response.status_code < 400):  # noqa: PLR2004
        response.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "sandbox; frame-ancestors 'none'"
        if response.mimetype not in INLINEABLE_ARTIFACT_CONTENT_TYPES:
            response.headers["Content-Disposition"] = (
                f'attachment; filename="{secure_filename(artifact.fname) or artifact_id}"'
            )
    return response


def register(app: Dash) -> None:
    """Register the `/artifact/<id>` download route onto `app`."""
    prefix = str(app.config.routes_pathname_prefix)  # pyrefly: ignore [unknown-argument-type]
    route = f"{prefix}artifact/<int:artifact_id>"
    app.server.add_url_rule(route, endpoint=route, view_func=_download, methods=["GET"])
