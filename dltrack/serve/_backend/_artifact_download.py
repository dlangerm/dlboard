"""
Serve an artifact's blob by its database id, e.g. `/artifact/42`.

A chart that shows an artifact (the image-series chart, say) only ever needs a URL a browser can
GET -- never the artifact's `ref` (a backend-specific blob location: `file:///...`, `s3://...`)
directly. Routing by id here, rather than by ref, keeps that ref entirely server-side: it goes
through the same soft-delete/scope checks as every other artifact read, and no chart plugin ever
has to know or care which `ArtifactStore` backend is in play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Response, abort
from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dltrack.serve._backend._data_store import get_artifact_store, get_data_store

if TYPE_CHECKING:
    from dash import Dash

_log = get_logger(__name__)

_IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
"""An artifact id's bytes never change once logged, so a browser may cache it forever -- but only
the browser (`private`): who may see it is per-user, so a shared proxy cache must never keep a copy."""


def artifact_url(artifact_id: int) -> str:
    """The URL a browser (or a chart's own `<img>`/`<a>`) fetches artifact `artifact_id` from."""
    return f"/artifact/{artifact_id}"


def _download(artifact_id: int) -> Response:
    """Look up `artifact_id`, then stream its blob. 404s for a missing or soft-deleted artifact."""
    artifact = get_data_store().get_artifact(artifact_id)
    if artifact is None:
        _log.warning("Artifact %s not found (missing or deleted)", artifact_id)
        abort(404)
    response = get_artifact_store().download_artifact(AnyUrl(artifact.ref))
    # A redirect to a short-lived presigned URL (`S3Blobs.download`'s presign mode) must never be
    # cached this long -- only the bytes behind it are immutable, not the redirect itself.
    if not (300 <= response.status_code < 400):  # noqa: PLR2004
        response.headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
    return response


def register(app: Dash) -> None:
    """Register the `/artifact/<id>` download route onto `app`."""
    prefix = str(app.config.routes_pathname_prefix)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    route = f"{prefix}artifact/<int:artifact_id>"
    app.server.add_url_rule(route, endpoint=route, view_func=_download, methods=["GET"])
