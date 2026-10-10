"""Tests for the Image artifact's tensor/array -> file conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import PIL.Image
import pytest
import torch

from dlboard.client.artifacts.image import Image, ImageFormat

if TYPE_CHECKING:
    from pathlib import Path


def _rgb_array() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


@pytest.mark.parametrize("fmt", ["png", "jpg"])
def test_to_artifact_writes_the_requested_format(tmp_path: Path, fmt: ImageFormat) -> None:
    img = Image(key="frame", image=_rgb_array(), step=3, tags={"split": "val"}, format=fmt)

    artifact, target = img.to_artifact(tmp_path, run_id=1, experiment_id=2)

    assert target.exists()
    assert target.suffix == f".{fmt}"
    assert artifact.key == "frame"
    assert artifact.fname == target.name
    assert artifact.run_id == 1
    assert artifact.experiment_id == 2
    assert artifact.step == 3
    assert artifact.tags == {"split": "val"}


def test_to_artifact_moves_a_torch_tensor_to_cpu_and_converts(tmp_path: Path) -> None:
    tensor = torch.zeros((4, 4, 3), dtype=torch.uint8)
    img = Image(key="frame", image=tensor, step=0)

    assert isinstance(img.image, torch.Tensor)
    assert not img.image.is_cuda

    _artifact, target = img.to_artifact(tmp_path, run_id=1, experiment_id=1)
    assert target.exists()


def test_the_default_encoding_keeps_a_hard_edged_mask_pixel_exact(tmp_path: Path) -> None:
    mask = np.zeros((16, 16, 3), dtype=np.uint8)
    mask[4:12, 4:12] = (255, 0, 0)

    _artifact, target = Image(key="mask", image=mask, step=0).to_artifact(tmp_path, run_id=1, experiment_id=1)

    assert target.suffix == ".png"
    assert np.array_equal(np.asarray(PIL.Image.open(target)), mask)
