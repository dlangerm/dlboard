# pyright: reportPrivateUsage=false
"""Tests for `S3Settings`/`S3Blobs` on their own: env parsing, download modes, read-only buckets."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
import requests
from pydantic import AnyHttpUrl, AnyUrl

from dlboard.plugins.data_stores import s3 as s3_module
from dlboard.plugins.data_stores._blob_store import BlobArtifactStore, RefAccess, blob_key
from dlboard.plugins.data_stores.s3 import S3Blobs, S3DownloadMode, S3Settings, _client

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dash import Dash
    from flask import Response
    from types_boto3_s3.client import S3Client


def _body(response: Response) -> bytes:
    """Read a streamed, `direct_passthrough` response's bytes, which `get_data()` can't."""
    return b"".join(cast("list[bytes]", list(response.response)))


def test_settings_are_read_from_s3_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("DLBOARD_S3_PREFIX", "runs")
    monkeypatch.setenv("DLBOARD_S3_ENDPOINT_URL", "http://minio.internal:9000")
    monkeypatch.setenv("DLBOARD_S3_ADDRESSING_STYLE", "path")
    monkeypatch.setenv("DLBOARD_S3_DOWNLOAD_MODE", "presign")
    monkeypatch.setenv("DLBOARD_S3_EXTRA_READ_BUCKETS", '["shared-bucket"]')

    settings = S3Settings()  # pyright: ignore[reportCallIssue] -- `bucket` is required, via DLBOARD_S3_BUCKET

    assert settings.bucket == "my-bucket"
    assert settings.prefix == "runs"
    assert settings.endpoint_url == AnyHttpUrl("http://minio.internal:9000")
    assert settings.addressing_style == "path"
    assert settings.download_mode is S3DownloadMode.PRESIGN
    assert settings.extra_read_buckets == frozenset({"shared-bucket"})


@pytest.mark.parametrize("raw_prefix", ["dlboard", "dlboard/", "/dlboard", "/dlboard/"])
def test_prefix_is_normalized_regardless_of_a_leading_or_trailing_slash(raw_prefix: str) -> None:
    """`_own_key` and `access` must agree on one shape, however `DLBOARD_S3_PREFIX` was spelled."""
    settings = S3Settings(bucket="b", prefix=raw_prefix)
    backend = S3Blobs(settings)

    assert settings.prefix == "dlboard"
    ref = backend.ref_for(blob_key(1, 1, "img", 0, "a.png"))
    assert backend.access(ref) is RefAccess.OWNED


def test_bucket_and_key_unquotes_a_percent_encoded_path() -> None:
    """`AnyUrl` stores a URL-encoded path; boto3's `Key=` needs the real, decoded S3 key."""
    backend = S3Blobs(S3Settings(bucket="b"))

    bucket, key = backend._bucket_and_key(AnyUrl("s3://b/dlboard/run%201/model.bin"))

    assert (bucket, key) == ("b", "dlboard/run 1/model.bin")


class _DeniedHeadClient:
    """A fake `S3Client` whose `head_object` always fails with `code`."""

    def __init__(self, code: str) -> None:
        self._code = code

    def head_object(self, **_: object) -> None:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": self._code}}, "HeadObject")


class _DeniedHeadBucketClient:
    """A fake `S3Client` whose `head_bucket` always fails, as if the bucket were unreachable."""

    def head_bucket(self, **_: object) -> None:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")


def _fake_client(client: object) -> Callable[[S3Settings], S3Client]:
    """A `_client`-shaped factory always returning `client`, for monkeypatching `s3_module._client`."""

    def _get(_settings: S3Settings) -> S3Client:
        return cast("S3Client", client)

    return _get


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "403", "AccessDenied"])
def test_exists_treats_an_access_denied_head_the_same_as_a_missing_key(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """
    Least-privilege credentials (no `s3:ListBucket`) make a `HeadObject` on a missing key 403, not
    404 -- indistinguishable here from a real permission error, and either way unconfirmable.
    """
    monkeypatch.setattr(s3_module, "_client", _fake_client(_DeniedHeadClient(code)))
    backend = S3Blobs(S3Settings(bucket="b"))

    assert backend.exists(AnyUrl("s3://b/dlboard/a.png")) is False


def test_exists_still_raises_on_an_unrelated_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3_module, "_client", _fake_client(_DeniedHeadClient("InternalError")))
    backend = S3Blobs(S3Settings(bucket="b"))

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        backend.exists(AnyUrl("s3://b/dlboard/a.png"))


def test_plug_fails_fast_on_an_unreachable_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad bucket/endpoint/credential should surface at startup, not on the first upload."""
    monkeypatch.setenv("DLBOARD_S3_BUCKET", "missing-bucket")
    monkeypatch.setattr(s3_module, "_client", _fake_client(_DeniedHeadBucketClient()))

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        s3_module.plug(cast("Dash", object()))


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

    assert response.content_length == len(b"hello")
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
def test_presign_mode_forces_a_non_inlineable_blob_to_download_as_an_attachment(
    s3_settings: S3Settings, tmp_path: Path
) -> None:
    """
    No bytes pass through this server in presign mode, so `_artifact_download.py`'s own headers
    (set on the 302 itself) never reach the browser's actual fetch of the object -- the presigned
    URL has to ask S3 for them directly, via `ResponseContentDisposition`/`ResponseContentType`.
    """
    backend = S3Blobs(s3_settings.model_copy(update={"download_mode": S3DownloadMode.PRESIGN}))
    ref = _write(backend, "evil.html", b"<script>alert(1)</script>", tmp_path)

    response = backend.download(ref)

    served = requests.get(response.headers["Location"], timeout=5)
    assert served.headers["Content-Disposition"] == "attachment"


@pytest.mark.s3
def test_presign_mode_does_not_force_an_inlineable_image_to_download(
    s3_settings: S3Settings, tmp_path: Path
) -> None:
    backend = S3Blobs(s3_settings.model_copy(update={"download_mode": S3DownloadMode.PRESIGN}))
    ref = _write(backend, "a.png", b"hello", tmp_path)

    response = backend.download(ref)

    served = requests.get(response.headers["Location"], timeout=5)
    assert "Content-Disposition" not in served.headers


@pytest.mark.s3
def test_a_read_only_allowlisted_bucket_is_downloadable_but_never_deleted(s3_settings: S3Settings) -> None:
    """
    A ref outside this store's own bucket/prefix, but allowlisted, is `READ_ONLY`.

    Linkable and downloadable, but `delete_artifact` must never actually remove it -- dlboard
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
