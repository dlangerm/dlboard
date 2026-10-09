"""Tests for the File artifact kind: uploading a file in place, tagged with what it is."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard.client.artifacts.file import File
from dlboard.models import FILE_KIND_TAG, FileKind

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("kind", list(FileKind))
def test_to_artifact_uploads_the_file_in_place_tagged_with_its_kind(tmp_path: Path, kind: FileKind) -> None:
    source = tmp_path / "epoch03.ckpt"
    source.write_bytes(b"weights")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    artifact, uploaded = File(
        key="checkpoints/epoch03", path=source, kind=kind, step=7, tags={"score": "0.9"}
    ).to_artifact(scratch, run_id=1, experiment_id=2)

    assert uploaded == source
    assert artifact.fname == "epoch03.ckpt"
    assert (artifact.key, artifact.run_id, artifact.experiment_id, artifact.step) == (
        "checkpoints/epoch03",
        1,
        2,
        7,
    )
    assert artifact.tags == {"score": "0.9", FILE_KIND_TAG: kind.value}
    assert list(scratch.iterdir()) == []
