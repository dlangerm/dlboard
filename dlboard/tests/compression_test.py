"""
End-to-end: the app compresses what it sends (#798) as zstd, Brotli or gzip, whichever the client takes, best first; never
a picture, never to a client on this same machine, and not at all when switched off.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING

import brotli
import pytest
from backports import zstd
from flask import Response

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
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
