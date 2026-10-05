"""`DLTRACK_TRUSTED_PROXIES` wraps the app's WSGI app in `ProxyFix` -- off unless opted into."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import request
from werkzeug.middleware.proxy_fix import ProxyFix

from dltrack.conftest import dispose_stores
from dltrack.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dltrack.plugins.data_stores import filesystem, sqlite
from dltrack.serve import app as build_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from dash import Dash


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dash:
    monkeypatch.setenv("DLTRACK_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLTRACK_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    return build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])


def test_proxy_fix_is_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build(tmp_path, monkeypatch)
    try:
        assert not isinstance(app.server.wsgi_app, ProxyFix)
    finally:
        dispose_stores(app)


def test_proxy_fix_wraps_the_wsgi_app_when_trusted_proxies_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLTRACK_TRUSTED_PROXIES", "1")
    app = _build(tmp_path, monkeypatch)
    try:
        assert isinstance(app.server.wsgi_app, ProxyFix)
    finally:
        dispose_stores(app)


def test_proxy_fix_then_trusts_the_forwarded_scheme_and_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLTRACK_TRUSTED_PROXIES", "1")
    app = _build(tmp_path, monkeypatch)
    try:
        seen: list[tuple[str, str | None]] = []

        @app.server.before_request
        def _record() -> None:
            seen.append((request.scheme, request.remote_addr))

        client = app.server.test_client()
        client.get("/healthz", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.2.3.4"})

        assert seen == [("https", "1.2.3.4")]
    finally:
        dispose_stores(app)
