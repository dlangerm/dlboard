"""Tests for `FSArtifactStore.delete_artifact`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import AnyUrl

from dltrack.plugins.data_stores.filesystem import FSArtifactStore

if TYPE_CHECKING:
    from pathlib import Path


def test_delete_artifact_removes_an_existing_blob(tmp_path: Path) -> None:
    store = FSArtifactStore.get_or_create(tmp_path, 1)
    target = tmp_path / "3" / "7" / "keyhash" / "0" / "namehash.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    ref = AnyUrl(f"{store.protocol}:///{target.relative_to(tmp_path)}")

    store.delete_artifact(ref)

    assert not target.exists()


def test_delete_artifact_is_a_noop_for_a_blob_that_is_already_gone(tmp_path: Path) -> None:
    store = FSArtifactStore.get_or_create(tmp_path, 1)
    ref = AnyUrl(f"{store.protocol}:///never/existed.png")

    store.delete_artifact(ref)  # must not raise
