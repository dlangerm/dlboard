"""A page loaded from an older version of the app is told to reload, not given a 500 per callback."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import pytest

from dlboard.conftest import dispose_stores
from dlboard.plugins import BUILTIN_BACKEND, LOCAL_AUTH
from dlboard.plugins.data_stores import filesystem, sqlite
from dlboard.serve import app as build_app
from dlboard.serve._jump import JUMP_MODAL_ID, JUMP_SELECT_ID

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

_UPDATE = "/_dash-update-component"
_OUTPUT = f"{JUMP_SELECT_ID}.data"
_MODAL_CLOSED = {"id": JUMP_MODAL_ID, "property": "opened", "value": False}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FlaskClient]:
    monkeypatch.setenv("DLBOARD_SQLITE_LOCATION", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("DLBOARD_ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    app = build_app([sqlite, filesystem, *LOCAL_AUTH, *BUILTIN_BACKEND])
    yield app.server.test_client()
    dispose_stores(app)


def _post(client: FlaskClient, **body: Any) -> int:  # noqa: ANN401
    return client.post(_UPDATE, json=body).status_code


def test_a_request_shaped_like_the_current_callback_goes_through(client: FlaskClient) -> None:
    status = _post(
        client,
        output=_OUTPUT,
        outputs={"id": JUMP_SELECT_ID, "property": "data"},
        inputs=[_MODAL_CLOSED],
        changedPropIds=[f"{JUMP_MODAL_ID}.opened"],
    )

    assert status != HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"output": _OUTPUT, "inputs": [_MODAL_CLOSED, _MODAL_CLOSED]}, id="more-inputs-than-it-has"
        ),
        pytest.param({"output": _OUTPUT, "inputs": []}, id="fewer-inputs-than-it-has"),
        pytest.param(
            {"output": _OUTPUT, "inputs": [_MODAL_CLOSED], "state": [_MODAL_CLOSED]}, id="extra-state"
        ),
        pytest.param({"output": "gone.children", "inputs": [_MODAL_CLOSED]}, id="callback-no-longer-exists"),
    ],
)
def test_a_request_from_an_older_page_is_answered_with_a_conflict(
    client: FlaskClient, body: dict[str, Any]
) -> None:
    response = client.post(_UPDATE, json=body)

    assert response.status_code == HTTPStatus.CONFLICT
    assert "reload" in response.get_json()["error"]


def test_a_malformed_request_is_left_to_dash(client: FlaskClient) -> None:
    assert client.post(_UPDATE, data="not json", content_type="text/plain").status_code != HTTPStatus.CONFLICT
