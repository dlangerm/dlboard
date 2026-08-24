# pyright: reportPrivateUsage=false
"""Tests for the REST client's request shaping (no real HTTP calls).

`log_artifact_batch` groups artifacts by key and must pair each artifact with
its own file by index; a past bug (two generators walked independently)
desynced this pairing, so it's worth pinning down explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dltrack import models
from dltrack.plugins.backend import basic_rest_backend as backend

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pytest


def test_create_path_joins_base_url_and_model_name() -> None:
    assert backend.create_path(models.Project, base_url="http://host:1") == "http://host:1/create/Project"


def test_create_path_handles_no_model() -> None:
    assert backend.create_path(None, base_url="http://host:1") == "http://host:1/create"


def test_entity_path_lowercases_model_name_and_defaults_placeholder() -> None:
    assert (
        backend.entity_path(models.Project, base_url="http://host:1")
        == "http://host:1/project/<int:entity_id>"
    )


def test_entity_path_accepts_a_real_id() -> None:
    assert backend.entity_path(models.Run, entity_id="7", base_url="http://host:1") == "http://host:1/run/7"


def test_client_sends_resolved_username_header_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "resolve_username", lambda: "alice")

    api = backend.BasicDltrackAPI()

    assert api._headers[backend.DLTRACK_USER_HEADER] == "alice"


def _new_artifact(key: str, fname: str) -> models.NewArtifact:
    return models.NewArtifact(key=key, fname=fname, run_id=1, experiment_id=1, step=0)


def test_create_project_sends_the_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, "resolve_username", lambda: "alice")

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

        def json(self) -> dict[str, Any]:
            return {
                "id": 1,
                "name": "p",
                "description": "d",
                "created_by": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "deleted_by": None,
                "deleted_at": None,
            }

    def fake_post(_url: str, *, json: dict[str, Any], headers: dict[str, str] | None = None) -> _FakeResponse:
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(backend.requests, "post", fake_post)

    backend.BasicDltrackAPI().create_project(models.NewProject(name="p", description="d"))

    assert captured["headers"] == {backend.DLTRACK_USER_HEADER: "alice"}


def test_log_artifact_batch_pairs_each_artifact_with_its_own_file(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [
        _new_artifact("img", "a.png"),
        _new_artifact("other", "b.bin"),
        _new_artifact("img", "c.png"),
    ]
    files = [Path("/tmp/a"), Path("/tmp/b"), Path("/tmp/c")]

    posted: list[dict[str, Any]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return

    def fake_post(_url: str, *, files: Iterable[Any], headers: dict[str, str] | None = None) -> _FakeResponse:
        posted.append({"files": list(files), "headers": headers})
        return _FakeResponse()

    def fake_open(path: Path, mode: str = "r") -> str:
        return path.name

    monkeypatch.setattr(backend.requests, "post", fake_post)
    monkeypatch.setattr(Path, "open", fake_open)

    backend.BasicDltrackAPI().log_artifact_batch(artifacts, files)

    # one POST per distinct key ("img", "other")
    assert len(posted) == 2
    img_call = next(p for p in posted if any(f[1][0] == "a.png" for f in p["files"]))
    fnames_sent = {f[1][0] for f in img_call["files"]}
    assert fnames_sent == {"a.png", "c.png"}
