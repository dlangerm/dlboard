# pyright: reportPrivateUsage=false
"""Tests for `S3Settings`/`S3Blobs` on their own: env parsing, download modes, read-only buckets."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
import requests
from pydantic import AnyHttpUrl, AnyUrl

from dltrack.plugins.data_stores._blob_store import BlobArtifactStore, RefAccess, blob_key
from dltrack.plugins.data_stores.s3 import S3Blobs, S3DownloadMode, S3Settings, _client

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Response


def _body(response: Response) -> bytes:
    """Read a streamed, `direct_passthrough` response's bytes, which `get_data()` can't."""
    return b"".join(cast("list[bytes]", list(response.response)))


def test_settings_are_read_from_s3_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    monkeypatch.setenv("S3_PREFIX", "runs")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio.internal:9000")
    monkeypatch.setenv("S3_ADDRESSING_STYLE", "path")
    monkeypatch.setenv("S3_DOWNLOAD_MODE", "presign")
    monkeypatch.setenv("S3_EXTRA_READ_BUCKETS", '["shared-bucket"]')

    settings = S3Settings()  # pyright: ignore[reportCallIssue] -- `bucket` is required, via S3_BUCKET

    assert settings.bucket == "my-bucket"
    assert settings.prefix == "runs"
    assert settings.endpoint_url == AnyHttpUrl("http://minio.internal:9000")
    assert settings.addressing_style == "path"
    assert settings.download_mode is S3DownloadMode.PRESIGN
    assert settings.extra_read_buckets == frozenset({"shared-bucket"})


def _write(backend: S3Blobs, key: str, content: bytes, tmp_path: Path) -> AnyUrl:
    staged = tmp_path / key
    staged.write_bytes(content)
    ref = backend.ref_for(blob_key(1, 1, "img", 0, key))
    backend.write(staged, ref)
    return ref


@pytest.mark.s3
def test_proxy_mode_streams_the_same_bytes_that_were_written(s3_settings: S3Settings, tmp_path: Path) -> None:
    backend = S3Blobs(s3_settings)
    ref = _write(backend, "a.png", b"hello", tmp_path)

    response = backend.download(ref)

    assert _body(response) == b"hello"


@pytest.mark.s3
def test_presign_mode_redirects_to_a_url_that_serves_the_same_bytes(
    s3_settings: S3Settings, tmp_path: Path
) -> None:
    backend = S3Blobs(s3_settings.model_copy(update={"download_mode": S3DownloadMode.PRESIGN}))
    ref = _write(backend, "a.png", b"hello", tmp_path)

    response = backend.download(ref)

    assert response.status_code == 302
    served = requests.get(response.headers["Location"], timeout=5)
    assert served.content == b"hello"


@pytest.mark.s3
def test_a_read_only_allowlisted_bucket_is_downloadable_but_never_deleted(s3_settings: S3Settings) -> None:
    """
    A ref outside this store's own bucket/prefix, but allowlisted, is `READ_ONLY`.

    Linkable and downloadable, but `delete_artifact` must never actually remove it -- dltrack
    didn't write it there, and has no business deleting something it doesn't own.
    """
    foreign_bucket = f"{s3_settings.bucket}-foreign"
    client = _client(s3_settings)  # pyright: ignore[reportArgumentType]
    client.create_bucket(
        Bucket=foreign_bucket,
        CreateBucketConfiguration={"LocationConstraint": s3_settings.region},  # pyright: ignore[reportArgumentType]
    )
    client.put_object(Bucket=foreign_bucket, Key="already-there.bin", Body=b"owned by someone else")
    ref = AnyUrl(f"s3://{foreign_bucket}/already-there.bin")
    backend = S3Blobs(s3_settings.model_copy(update={"extra_read_buckets": frozenset({foreign_bucket})}))
    store = BlobArtifactStore.get_or_create(backend, 1)

    assert backend.access(ref) is RefAccess.READ_ONLY
    assert _body(store.download_artifact(ref)) == b"owned by someone else"

    store.delete_artifact(ref)

    assert backend.exists(ref)
