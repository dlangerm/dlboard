"""
End-to-end: the app compresses what it sends (#798) as zstd, Brotli or gzip, whichever the client takes, best first; never
a picture, never to a client on this same machine, and not at all when switched off. And it reads a request body a client
compressed (#799), up to the same limit as any other.
"""

from __future__ import annotations

import gzip
import json
from contextlib import contextmanager
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING

import brotli
import pytest
from flask import Response

from dlboard._content_encoding import ContentEncoding, compress, decompress
from dlboard._wire import WHOAMI_PATH, Resource, get_or_create_path
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH, PASSWORD_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import add_public_route
from dlboard.serve import app as build_app
from dlboard.serve._backend._compression import ResponseEncoding

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

    from dlboard.models import PluginProtocol

_REMOTE = {"REMOTE_ADDR": "203.0.113.7"}
_PICTURE = b"\x89PNG" + bytes(range(256)) * 8
_EVERY_ENCODING = {"Accept-Encoding": ", ".join(ResponseEncoding)}


@contextmanager
def _serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: list[PluginProtocol] = LOCAL_AUTH
) -> Generator[FlaskClient]:
    """A test client of an app with `auth`, which also serves a picture at `/picture`."""
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *auth, *BUILTIN_BACKEND])
    add_public_route(app, "picture", lambda: Response(_PICTURE, content_type="image/png"), ["GET"])
    try:
        yield app.server.test_client()
    finally:
        dispose_stores(app)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    with _serving(tmp_path, monkeypatch) as serving:
        yield serving


@pytest.mark.parametrize(
    ("offered", "chosen", "decode"),
    [
        (
            "gzip, deflate, br, zstd",
            ResponseEncoding.ZSTD,
            partial(decompress, limit=10**7),
        ),  # what a browser offers over HTTPS
        ("gzip, deflate, br", ResponseEncoding.BR, brotli.decompress),
        ("gzip, deflate", ResponseEncoding.GZIP, gzip.decompress),  # what a browser offers over plain HTTP
    ],
)
def test_a_response_is_compressed_the_best_way_the_client_takes(
    client: FlaskClient, offered: str, chosen: ResponseEncoding, decode: Callable[[bytes], bytes]
) -> None:
    plain = client.get("/_dash-layout", environ_base=_REMOTE, headers={"Accept-Encoding": "identity"})
    compressed = client.get("/_dash-layout", environ_base=_REMOTE, headers={"Accept-Encoding": offered})

    assert compressed.headers["Content-Encoding"] == chosen
    assert decode(compressed.data) == plain.data
    assert len(compressed.data) < len(plain.data)


@pytest.mark.parametrize(
    ("path", "environ"),
    [
        ("/picture", _REMOTE),
        ("/_dash-layout", {"REMOTE_ADDR": "127.0.0.1"}),
        ("/_dash-layout", {"REMOTE_ADDR": "::1"}),
    ],
    ids=["a picture, already compressed", "to this machine", "to this machine over IPv6"],
)
def test_some_responses_are_sent_as_they_are(client: FlaskClient, path: str, environ: dict[str, str]) -> None:
    response = client.get(path, environ_base=environ, headers=_EVERY_ENCODING)

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers


def test_a_client_a_proxy_names_as_unknown_is_compressed_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`X-Forwarded-For: unknown` (as Squid sends it) is no address, and so not this machine's: never a 500."""
    monkeypatch.setenv("DLBOARD_TRUSTED_PROXIES", "1")
    with _serving(tmp_path, monkeypatch) as client:
        response = client.get(
            "/_dash-layout", environ_base=_REMOTE, headers={**_EVERY_ENCODING, "X-Forwarded-For": "unknown"}
        )

    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == ResponseEncoding.ZSTD


def test_nothing_is_compressed_when_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_COMPRESS_RESPONSES", "false")
    with _serving(tmp_path, monkeypatch) as client:
        response = client.get("/_dash-layout", environ_base=_REMOTE, headers=_EVERY_ENCODING)

    assert "Content-Encoding" not in response.headers


def test_a_compressed_request_body_is_read_as_the_plain_one(client: FlaskClient) -> None:
    """#799: and `whoami` says so, so that a client knows it may send one."""
    assert client.get(f"/{WHOAMI_PATH}").get_json()["accepted_encodings"] == list(ContentEncoding)
    body = json.dumps({"name": "squeezed", "description": "a long one " * 200}).encode()
    response = client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        data=compress(body),
        headers={"Content-Type": "application/json", "Content-Encoding": ContentEncoding.ZSTD},
    )

    assert response.status_code == HTTPStatus.OK
    assert response.get_json()["name"] == "squeezed"


@pytest.mark.parametrize(
    ("encoding", "body", "status"),
    [
        (ResponseEncoding.GZIP, gzip.compress(b"{}"), HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
        (ContentEncoding.ZSTD, b"not zstd at all", HTTPStatus.BAD_REQUEST),
        (ContentEncoding.ZSTD, compress(b"[" + b" " * 2_000_000 + b"]"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    ],
    ids=["an encoding it doesn't take", "not what it says it is", "too large once decompressed"],
)
def test_a_request_body_that_cannot_be_decompressed_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: ContentEncoding | ResponseEncoding,
    body: bytes,
    status: HTTPStatus,
) -> None:
    """Past 1 MB (`DLBOARD_MAX_UPLOAD_MB`) once decompressed is too large, as an uncompressed body is."""
    monkeypatch.setenv("DLBOARD_MAX_UPLOAD_MB", "1")
    with _serving(tmp_path, monkeypatch) as client:
        response = client.post(
            f"/{get_or_create_path(Resource.PROJECTS)}",
            data=body,
            headers={"Content-Type": "application/json", "Content-Encoding": encoding},
        )

    assert response.status_code == status


def test_nothing_is_decompressed_for_a_request_the_gate_turns_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decompressing comes after signing in: a caller with no credentials is refused before its body is looked at."""
    monkeypatch.setenv("DLBOARD_SECRET_KEY", "test-secret-at-least-32-characters-long")
    with _serving(tmp_path, monkeypatch, PASSWORD_AUTH) as client:
        response = client.post(
            f"/{get_or_create_path(Resource.PROJECTS)}",
            data=b"not zstd at all",
            headers={"Content-Type": "application/json", "Content-Encoding": ContentEncoding.ZSTD},
        )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
