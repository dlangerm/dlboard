"""
End-to-end: a deployment that a reverse proxy signs people in to (`dlboard.plugins.TRUSTED_HEADER_AUTH`, #800).

The proxy's headers are believed from the proxy alone, by the connection's own address; the same headers from anyone else
are refused, an `X-Forwarded-For` naming the proxy included.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from dlboard import models
from dlboard._wire import WHOAMI_PATH
from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, TRUSTED_HEADER_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve import get_system_data_store
from dlboard.serve._backend._api_tokens import mint_api_token
from dlboard.serve._backend._app_slot import DASH_APP_EXTENSION

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

_PROXY, _ELSEWHERE = {"REMOTE_ADDR": "10.0.0.2"}, {"REMOTE_ADDR": "10.0.9.9"}
_ALICE = {
    "X-Forwarded-User": "alice",
    "X-Forwarded-Email": "alice@example.com",
    "X-Forwarded-Groups": "ml, ops",
}


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "DLBOARD_SQLITE_LOCATION": str(tmp_path / "db.sqlite"),
        "DLBOARD_ARTIFACT_STORE_LOCATION": str(tmp_path / "artifacts"),
        "DLBOARD_SECRET_KEY": "test-secret-at-least-32-characters-long",
        "DLBOARD_TRUSTED_HEADER_ISSUER": "sso.example.com",
        "DLBOARD_TRUSTED_HEADER_PROXIES": "10.0.0.0/30, 192.0.2.7",
        "DLBOARD_ADMIN_GROUPS": "ops",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    _settings(tmp_path, monkeypatch)
    app = build_app([sqlite, filesystem, *TRUSTED_HEADER_AUTH, *BUILTIN_BACKEND])
    yield app.server.test_client()
    dispose_stores(app)


def test_the_proxy_signs_a_person_in_with_their_email_and_groups(client: FlaskClient) -> None:
    """A group in `DLBOARD_ADMIN_GROUPS` makes them an admin, as with any identity provider that has groups."""
    response = client.get(f"/{WHOAMI_PATH}", environ_base=_PROXY, headers=_ALICE)

    assert response.status_code == 200
    assert response.get_json()["username"] == "alice"
    user = get_system_data_store(client.application.extensions[DASH_APP_EXTENSION]).find_user("alice")
    assert user is not None
    assert (user.issuer, user.subject, user.email) == ("sso.example.com", "alice", "alice@example.com")
    assert models.Scope.ALL in user.scopes


@pytest.mark.parametrize(
    ("environ", "headers", "status"),
    [
        pytest.param(_ELSEWHERE, _ALICE, 403, id="the-headers-from-anyone-but-the-proxy"),
        pytest.param(
            _ELSEWHERE,
            {**_ALICE, "X-Forwarded-For": "10.0.0.2"},
            403,
            id="claiming-to-come-through-the-proxy",
        ),
        pytest.param(_ELSEWHERE, {"X-Forwarded-Groups": "ops"}, 403, id="any-one-of-the-headers"),
        pytest.param(_PROXY, {}, 401, id="the-proxy-naming-no-one"),
    ],
)
def test_no_one_is_signed_in_but_whom_the_proxy_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    headers: dict[str, str],
    status: int,
) -> None:
    """With `ProxyFix` on (`DLBOARD_TRUSTED_PROXIES`), as behind a proxy: `X-Forwarded-For` moves the remote address."""
    _settings(tmp_path, monkeypatch)
    monkeypatch.setenv("DLBOARD_TRUSTED_PROXIES", "1")
    app = build_app([sqlite, filesystem, *TRUSTED_HEADER_AUTH, *BUILTIN_BACKEND])
    client: FlaskClient = app.server.test_client()

    response = client.get(f"/{WHOAMI_PATH}", environ_base=environ, headers=headers)
    dispose_stores(app)

    assert response.status_code == status


def test_an_api_token_works_from_anywhere_without_the_proxy(client: FlaskClient) -> None:
    client.get(f"/{WHOAMI_PATH}", environ_base=_PROXY, headers=_ALICE)
    store = get_system_data_store(client.application.extensions[DASH_APP_EXTENSION])
    alice = store.find_user("alice")
    assert alice is not None
    _, token = mint_api_token(store, alice, "training")

    response = client.get(
        f"/{WHOAMI_PATH}", environ_base=_ELSEWHERE, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.get_json()["username"] == "alice"


@pytest.mark.parametrize("missing", ["DLBOARD_TRUSTED_HEADER_ISSUER", "DLBOARD_TRUSTED_HEADER_PROXIES"])
def test_a_deployment_that_names_no_issuer_or_no_proxy_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """There is no "believe anyone" default."""
    _settings(tmp_path, monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError, match=missing):
        build_app([sqlite, filesystem, *TRUSTED_HEADER_AUTH, *BUILTIN_BACKEND])
