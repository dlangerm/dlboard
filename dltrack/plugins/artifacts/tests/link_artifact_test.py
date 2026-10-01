"""Tests for the Link artifact kind: pairing metadata with an existing ref, no bytes written."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import AnyUrl

from dltrack.plugins.artifacts.link import Link

if TYPE_CHECKING:
    from pathlib import Path


def test_to_artifact_pairs_metadata_with_the_ref_unchanged(tmp_path: Path) -> None:
    ref = AnyUrl("s3://bucket/prefix/already-uploaded.png")
    link = Link(key="frame", ref=ref, step=3, tags={"split": "val"})

    artifact, returned_ref = link.to_artifact(tmp_path, run_id=1, experiment_id=2)

    assert returned_ref == ref
    assert artifact.key == "frame"
    assert artifact.fname == "already-uploaded.png"
    assert artifact.run_id == 1
    assert artifact.experiment_id == 2
    assert artifact.step == 3
    assert artifact.tags == {"split": "val"}


def test_to_artifact_writes_nothing_under_local_temp(tmp_path: Path) -> None:
    Link(key="frame", ref=AnyUrl("file:///a/b.png"), step=0).to_artifact(tmp_path, run_id=1, experiment_id=1)

    assert list(tmp_path.iterdir()) == []
