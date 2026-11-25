"""
End-to-end: the app compresses what it sends (#798) as zstd, Brotli or gzip, whichever the client takes, best first; never
a picture, never to a client on this same machine, and not at all when switched off. And it reads a request body a client
compressed (#799), up to the same limit as any other.
"""

from __future__ import annotations

import gzip
import json
from typing import TYPE_CHECKING

import brotli
import pytest
from backports import zstd
from flask import Response

from dlboard._content_encoding import ContentEncoding, encode
from dlboard._wire import WHOAMI_PATH, Resource, get_or_create_path
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH, PASSWORD_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import add_public_route
from dlboard.serve import app as build_app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

_REMOTE = {"REMOTE_ADDR": "203.0.113.7"}
_PICTURE = b"\x89PNG" + bytes(range(256)) * 8


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    add_public_route(app, "picture", lambda: Response(_PICTURE, content_type="image/png"), ["GET"])
    yield app.server.test_client()
    dispose_stores(app)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    yield from _client(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("offered", "chosen", "decode"),
    [
        ("gzip, deflate, br, zstd", "zstd", zstd.decompress),  # what a browser offers over HTTPS
        ("gzip, deflate, br", "br", brotli.decompress),
        ("gzip, deflate", "gzip", gzip.decompress),  # what a browser offers over plain HTTP
    ],
)
def test_a_response_is_compressed_the_best_way_the_client_takes(
    client: FlaskClient, offered: str, chosen: str, decode: Callable[[bytes], bytes]
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
    response = client.get(path, environ_base=environ, headers={"Accept-Encoding": "zstd, br, gzip"})

    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers


def test_nothing_is_compressed_when_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLBOARD_COMPRESS_RESPONSES", "false")
    for client in _client(tmp_path, monkeypatch):
        response = client.get("/_dash-layout", environ_base=_REMOTE, headers={"Accept-Encoding": "zstd"})
        assert "Content-Encoding" not in response.headers


@pytest.mark.parametrize("encoding", list(ContentEncoding))
def test_a_compressed_request_body_is_read_as_the_plain_one(
    client: FlaskClient, encoding: ContentEncoding
) -> None:
    """#799: and `whoami` says so, so that a client knows it may send one."""
    assert client.get(f"/{WHOAMI_PATH}").get_json()["accepted_encodings"] == list(ContentEncoding)
    body = json.dumps({"name": "squeezed", "description": "a long one " * 200}).encode()
    response = client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        data=encode(body, encoding),
        headers={"Content-Type": "application/json", "Content-Encoding": encoding},
    )

    assert response.status_code == 200
    assert response.get_json()["name"] == "squeezed"


@pytest.mark.parametrize(
    ("encoding", "body", "status"),
    [
        ("br", b"{}", 415),  # one this server doesn't take
        (ContentEncoding.ZSTD, b"not zstd at all", 400),
        (
            ContentEncoding.GZIP,
            encode(b"[" + b" " * 2_000_000 + b"]", ContentEncoding.GZIP),
            413,
        ),  # past 1 MB
    ],
    ids=["an encoding it doesn't take", "not what it says it is", "too large once decompressed"],
)
def test_a_request_body_that_cannot_be_decompressed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str, body: bytes, status: int
) -> None:
    monkeypatch.setenv("DLBOARD_MAX_UPLOAD_MB", "1")
    for client in _client(tmp_path, monkeypatch):
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
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    monkeypatch.setenv("DLBOARD_SECRET_KEY", "test-secret-at-least-32-characters-long")
    app = build_app([sqlite, filesystem, *PASSWORD_AUTH, *BUILTIN_BACKEND])
    client: FlaskClient = app.server.test_client()
    response = client.post(
        f"/{get_or_create_path(Resource.PROJECTS)}",
        data=b"not zstd at all",
        headers={"Content-Type": "application/json", "Content-Encoding": ContentEncoding.ZSTD},
    )
    dispose_stores(app)

    assert response.status_code == 401
