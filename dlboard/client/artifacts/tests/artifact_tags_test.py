"""Tests for how every artifact kind turns the tags a caller gave it into the text that is stored."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
from pydantic import AnyUrl

from dlboard.client.artifacts.figure import Figure
from dlboard.client.artifacts.file import File
from dlboard.client.artifacts.image import Image
from dlboard.client.artifacts.link import Link
from dlboard.models import FileKind, format_tags

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from dlboard.models import AnyArtifact, TagValue

_TAGS: Mapping[str, TagValue] = {"score": 0.140984, "epoch": 3, "best": True, "split": "val"}
_STORED = {"score": "0.1410", "epoch": "3", "best": "true", "split": "val"}


def _image(tags: Mapping[str, TagValue], _path: Path) -> AnyArtifact:
    return Image(key="k", image=np.zeros((2, 2, 3), dtype=np.uint8), step=0, tags=tags)


def _figure(tags: Mapping[str, TagValue], _path: Path) -> AnyArtifact:
    return Figure(key="k", data=b"png", step=0, tags=tags)


def _file(tags: Mapping[str, TagValue], path: Path) -> AnyArtifact:
    return File(key="k", path=path, kind=FileKind.CHECKPOINT, step=0, tags=tags)


def _link(tags: Mapping[str, TagValue], _path: Path) -> AnyArtifact:
    return Link(key="k", ref=AnyUrl("s3://bucket/k.png"), step=0, tags=tags)


@pytest.mark.parametrize("build", [_image, _figure, _file, _link])
def test_every_artifact_kind_stores_numeric_tags_as_the_same_text(
    build: Callable[[Mapping[str, TagValue], Path], AnyArtifact], tmp_path: Path
) -> None:
    artifact, _target = build(_TAGS, tmp_path / "f.bin").to_artifact(tmp_path, run_id=1, experiment_id=1)

    assert {k: v for k, v in artifact.tags.items() if k != "file_kind"} == _STORED


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.140984, "0.1410"),
        (0.14377, "0.1438"),
        (5.0, "5.000"),
        (1e-05, "1.000e-05"),
        (12, "12"),
        (True, "true"),
        (False, "false"),
        ("0.5", "0.5"),
        ("", ""),
    ],
)
def test_a_tag_value_is_formatted_the_same_way_whoever_logged_it(value: TagValue, text: str) -> None:
    assert format_tags({"k": value}) == {"k": text}
