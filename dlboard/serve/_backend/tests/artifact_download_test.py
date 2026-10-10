"""Unit tests for `_artifact_download`'s own logic -- whether the download gets cached forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from flask import Response

from dlboard import models
from dlboard.serve._backend import _artifact_download

if TYPE_CHECKING:
    from pydantic import AnyUrl


class _FakeDataStore:
    """Resolves any id to one fixed artifact, standing in for a real `DataStore`."""

    def __init__(self, artifact: models.Artifact) -> None:
        self._artifact = artifact

    def get_artifact(self, artifact_id: int) -> models.Artifact:
        del artifact_id
        return self._artifact


class _FakeArtifactStore:
    """Hands back one fixed `Response`, standing in for a real `ArtifactStore`."""

    def __init__(self, response: Response) -> None:
        self._response = response

    def download_artifact(self, ref: AnyUrl) -> Response:
        del ref
        return self._response


def _artifact() -> models.Artifact:
    return models.Artifact(key="img", fname="a.png", run_id=1, experiment_id=1, step=0, ref="file:///a.png")


def test_a_proxied_download_gets_the_immutable_cache_control_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_artifact_download, "get_data_store", lambda: _FakeDataStore(_artifact()))
    monkeypatch.setattr(_artifact_download, "get_artifact_store", lambda: _FakeArtifactStore(Response(b"x")))

    response = _artifact_download._download(1)

    assert response.headers["Cache-Control"] == _artifact_download._IMMUTABLE_CACHE_CONTROL


def test_a_proxied_download_always_gets_nosniff_and_a_sandboxed_csp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_artifact_download, "get_data_store", lambda: _FakeDataStore(_artifact()))
    monkeypatch.setattr(_artifact_download, "get_artifact_store", lambda: _FakeArtifactStore(Response(b"x")))

    response = _artifact_download._download(1)

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox; frame-ancestors 'none'"


def test_an_inlineable_image_is_not_forced_to_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_artifact_download, "get_data_store", lambda: _FakeDataStore(_artifact()))
    monkeypatch.setattr(
        _artifact_download,
        "get_artifact_store",
        lambda: _FakeArtifactStore(Response(b"x", content_type="image/png")),
    )

    response = _artifact_download._download(1)

    assert "Content-Disposition" not in response.headers


def test_a_non_inlineable_artifact_is_forced_to_download_as_an_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Regression: an artifact's `fname`/content type is entirely client-supplied, so anyone who can
    log an artifact could otherwise upload HTML or SVG crafted to run script in this origin the
    moment someone else opens it -- `nosniff` plus this is what stops a browser rendering it instead
    of downloading it.
    """
    html_artifact = models.Artifact(
        key="k", fname="evil.html", run_id=1, experiment_id=1, step=0, ref="file:///evil.html"
    )
    monkeypatch.setattr(_artifact_download, "get_data_store", lambda: _FakeDataStore(html_artifact))
    monkeypatch.setattr(
        _artifact_download,
        "get_artifact_store",
        lambda: _FakeArtifactStore(Response(b"<script>alert(1)</script>", content_type="text/html")),
    )

    response = _artifact_download._download(1)

    assert response.headers["Content-Disposition"] == 'attachment; filename="evil.html"'


def test_a_presigned_redirect_is_never_given_the_immutable_cache_control_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A `BlobBackend.download` in presign mode 302s to a short-lived URL, not the artifact's bytes.

    Stamping that redirect itself with a year-long `Cache-Control` (the bytes' own lifetime, not
    the presigned URL's) would have a browser replay an expired redirect long after it 403s.
    """
    monkeypatch.setattr(_artifact_download, "get_data_store", lambda: _FakeDataStore(_artifact()))
    presigned_redirect = Response(status=302)
    presigned_redirect.headers["Location"] = "https://example.com/presigned"
    monkeypatch.setattr(
        _artifact_download, "get_artifact_store", lambda: _FakeArtifactStore(presigned_redirect)
    )

    response = _artifact_download._download(1)

    assert "Cache-Control" not in response.headers
    assert "X-Content-Type-Options" not in response.headers
    assert "Content-Security-Policy" not in response.headers
    assert "Content-Disposition" not in response.headers


_LOGGED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _artifact_with(ref: str = "file:///blobs/a.png", logged_at: datetime = _LOGGED_AT) -> models.Artifact:
    return models.Artifact(
        id=5, key="img", fname="a.png", run_id=1, experiment_id=1, step=0, ref=ref, created_at=logged_at
    )


@pytest.mark.parametrize(
    ("other", "same_artifact"),
    [
        pytest.param(_artifact_with(), True, id="the-same-artifact"),
        pytest.param(_artifact_with(ref="file:///blobs/b.png"), False, id="another-blob-under-the-same-id"),
        pytest.param(
            _artifact_with(logged_at=_LOGGED_AT + timedelta(seconds=1)),
            False,
            id="logged-later-under-the-same-id",
        ),
    ],
)
def test_an_artifacts_url_token_changes_when_its_id_is_reused_by_another_artifact(
    other: models.Artifact, *, same_artifact: bool
) -> None:
    """
    Ids restart in a fresh database, and the browser caches `/artifact/<id>` for a year.

    Reproduced by running two databases one after the other on the same address under one browser
    profile: the second's pages showed the first's images, straight from the browser cache.
    """
    assert (
        _artifact_download.artifact_version(_artifact_with()) == _artifact_download.artifact_version(other)
    ) is (same_artifact)
