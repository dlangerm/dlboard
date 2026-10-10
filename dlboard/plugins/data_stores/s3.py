"""
S3 artifact store: a `BlobBackend` over any S3-protocol object store (AWS, MinIO, VAST, ...).

Nothing here is AWS-specific -- `endpoint_url` points this at any S3-compatible endpoint, and
`addressing_style` switches to path-style for stores (MinIO, most on-prem/VAST deployments) that
don't support virtual-hosted-style bucket addressing. Credentials left unset fall back to boto3's
own default chain (environment, shared config/profile, instance/container role).
"""

from __future__ import annotations

import functools
import mimetypes
import urllib.parse
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from flask import Response, redirect
from pydantic import AnyHttpUrl, AnyUrl, NonNegativeInt, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.models import INLINEABLE_ARTIFACT_CONTENT_TYPES
from dlboard.plugins.data_stores._blob_store import RefAccess, plug_blob_store

if TYPE_CHECKING:
    from dash import Dash
    from types_boto3_s3.client import S3Client


class S3DownloadMode(StrEnum):
    """How `S3Blobs.download` hands an artifact's bytes to a browser."""

    PROXY = "proxy"
    """Stream the object through this server. Always reachable, even from an endpoint
    (MinIO in a private network, say) browsers themselves can't reach."""

    PRESIGN = "presign"
    """302-redirect to a short-lived presigned URL. No bytes pass through this server, but the
    endpoint must be directly reachable from the browser."""


class S3Settings(BaseSettings):
    """Connection and behavior settings, read from `DLBOARD_S3_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_S3_", frozen=True)

    bucket: str
    prefix: str = "dlboard"
    """Every blob this store writes lives under `s3://bucket/prefix/...`."""

    @field_validator("prefix")
    @classmethod
    def _strip_slashes(cls, value: str) -> str:
        """Normalize away a leading/trailing slash, so `_own_key` and `access` agree on one shape."""
        return value.strip("/")

    endpoint_url: AnyHttpUrl | None = None
    """Left unset, boto3 talks to AWS; set it to point at any other S3-protocol endpoint
    (MinIO, VAST, ...)."""
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None
    session_token: str | None = None
    """Left unset along with the two above, boto3 falls back to its own default credential chain
    (environment, shared config/profile, instance/container role)."""

    addressing_style: Literal["auto", "path", "virtual"] = "auto"
    """MinIO and most on-prem/VAST deployments need `path` -- they don't support virtual-hosted
    bucket addressing (`<bucket>.<endpoint>/...`), only `<endpoint>/<bucket>/...`."""
    verify_tls: bool = True
    ca_bundle: Path | None = None
    """A CA bundle for a self-signed or private-CA endpoint. Implies TLS verification."""

    download_mode: S3DownloadMode = S3DownloadMode.PROXY
    presign_ttl_s: PositiveInt = 300

    extra_read_buckets: frozenset[str] = frozenset()
    """Other buckets this store's credentials may read (and link/download) from, but never
    write to or delete from -- for a blob a client put somewhere outside this store's own
    `bucket`/`prefix` itself."""

    queue_size: NonNegativeInt = 100


# S3Settings is a frozen (so hashable) pydantic model; pyrefly does not see the `__hash__` pydantic generates.
@functools.cache
def _client(settings: S3Settings) -> S3Client:
    """
    Get this process's boto3 client for `settings`, built once and reused.

    Cached by value (`S3Settings` is frozen, hence hashable), not by identity: the write worker
    gets its own freshly unpickled `S3Settings` with every call, and this still reuses one client
    per distinct settings rather than opening a new connection pool per blob.
    """
    import boto3
    from botocore.config import Config

    session = boto3.session.Session(
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key.get_secret_value()
        if settings.secret_access_key
        else None,
        aws_session_token=settings.session_token,
        region_name=settings.region,
    )
    # `types-boto3[s3]` alone (not the much larger "full" stub set covering every AWS service)
    # leaves `Session.client`'s own overloads partially unknown to pyrefly, hence the ignore --
    # the one overload that actually matches `"s3"` still resolves to the real `S3Client`.
    return session.client(
        "s3",
        endpoint_url=str(settings.endpoint_url) if settings.endpoint_url else None,
        config=Config(s3={"addressing_style": settings.addressing_style}),
        verify=str(settings.ca_bundle) if settings.ca_bundle else settings.verify_tls,
    )


class S3Blobs:
    """Keep every blob under `s3://settings.bucket/settings.prefix/...`."""

    def __init__(self, settings: S3Settings) -> None:
        """
        Initialize the backend.

        Only `settings` (plain, picklable data) is stored -- never the boto3 client itself, which
        `_client` builds fresh in whichever process needs it.
        """
        self._settings = settings

    def _bucket_and_key(self, ref: AnyUrl) -> tuple[str, str]:
        """`ref`'s bucket and key, for one of boto3's own `Bucket=`/`Key=` calls."""
        assert ref.host is not None, f"not a bucket-having s3 ref: {ref}"
        # `ref.path` is URL-percent-encoded (pydantic's `AnyUrl`); the real S3 key isn't.
        return ref.host, urllib.parse.unquote((ref.path or "").lstrip("/"))

    def _own_key(self, key: PurePosixPath) -> str:
        return str(PurePosixPath(self._settings.prefix) / key)

    def ref_for(self, key: PurePosixPath) -> AnyUrl:
        """The ref a freshly written blob at `key` will get."""
        return AnyUrl(f"s3://{self._settings.bucket}/{self._own_key(key)}")

    def access(self, ref: AnyUrl) -> RefAccess | None:
        """`OWNED` inside `bucket`/`prefix`, `READ_ONLY` in an allowlisted bucket, else `None`."""
        if ref.scheme != "s3" or ref.host is None:
            return None
        own_prefix = f"{self._settings.prefix}/" if self._settings.prefix else ""
        path = (ref.path or "").lstrip("/")
        if ref.host == self._settings.bucket and path.startswith(own_prefix):
            return RefAccess.OWNED
        if ref.host in self._settings.extra_read_buckets:
            return RefAccess.READ_ONLY
        return None

    def exists(self, ref: AnyUrl) -> bool:
        """Whether a blob is already stored at `ref`."""
        from botocore.exceptions import ClientError

        bucket, key = self._bucket_and_key(ref)
        try:
            _client(self._settings).head_object(Bucket=bucket, Key=key)  # pyrefly: ignore [bad-argument-type]
        except ClientError as exc:
            # Least-privilege credentials (no s3:ListBucket) make AWS answer a HEAD on a missing
            # key with 403 rather than 404 -- indistinguishable here from a real permission error,
            # and either way this store can't confirm the blob, so treat both as "not found".
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "403", "AccessDenied"):
                return False
            raise
        return True

    def write(self, staged: Path, ref: AnyUrl) -> None:
        """Upload `staged`'s bytes to `ref`, then remove `staged`."""
        bucket, key = self._bucket_and_key(ref)
        content_type, _ = mimetypes.guess_type(key)
        try:
            _client(self._settings).upload_file(  # pyrefly: ignore [bad-argument-type]
                str(staged),
                Bucket=bucket,
                Key=key,
                ExtraArgs={"ContentType": content_type} if content_type else {},
            )
        finally:
            staged.unlink(missing_ok=True)

    def download(self, ref: AnyUrl) -> Response:
        """Serve `ref`'s bytes to a browser, proxied or presigned per `download_mode`."""
        bucket, key = self._bucket_and_key(ref)
        client = _client(self._settings)  # pyrefly: ignore [bad-argument-type]
        match self._settings.download_mode:
            case S3DownloadMode.PRESIGN:
                content_type, _ = mimetypes.guess_type(key)
                params: dict[str, str] = {"Bucket": bucket, "Key": key}
                # The browser fetches the object straight from S3 under whatever headers the
                # presigned URL asks for -- these two are what `_artifact_download.py` sets
                # directly in `PROXY` mode, carried over here since this response is just a
                # redirect to it, not the bytes themselves. `nosniff`/CSP can't travel this way
                # (S3 has no such response-header override), which is why `PRESIGN` is opt-in, not
                # the default -- see `S3DownloadMode`.
                if content_type:
                    params["ResponseContentType"] = content_type
                if content_type not in INLINEABLE_ARTIFACT_CONTENT_TYPES:
                    params["ResponseContentDisposition"] = "attachment"
                url = client.generate_presigned_url(
                    "get_object", Params=params, ExpiresIn=self._settings.presign_ttl_s
                )
                return cast("Response", redirect(url))
            case S3DownloadMode.PROXY:
                obj = client.get_object(Bucket=bucket, Key=key)
                body = obj["Body"]
                response = Response(
                    body.iter_chunks(chunk_size=256 * 1024),
                    content_type=obj.get("ContentType") or "application/octet-stream",
                    direct_passthrough=True,
                )
                response.content_length = obj["ContentLength"]
                response.call_on_close(body.close)
                return response

    def delete(self, ref: AnyUrl) -> None:
        """Permanently delete the blob at `ref`. Idempotent: S3's own `delete_object` always is."""
        bucket, key = self._bucket_and_key(ref)
        _client(self._settings).delete_object(Bucket=bucket, Key=key)  # pyrefly: ignore [bad-argument-type]


def plug(app: Dash) -> None:
    """Plugin content."""
    settings = S3Settings()
    # Fail fast on a bad bucket/endpoint/credential instead of only discovering it on the first upload.
    _client(settings).head_bucket(Bucket=settings.bucket)  # pyrefly: ignore [bad-argument-type]
    plug_blob_store(app, S3Blobs(settings), settings.queue_size)
