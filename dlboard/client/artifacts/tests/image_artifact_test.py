# pyright: reportPrivateUsage=false
"""Tests for the Image artifact's tensor/array -> file conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import torch

from dlboard.client.artifacts.image import Image

if TYPE_CHECKING:
    from pathlib import Path


def _rgb_array() -> np.ndarray:
    return np.zeros((4, 4, 3), dtype=np.uint8)


def test_to_artifact_writes_a_jpg_from_a_numpy_array(tmp_path: Path) -> None:
    img = Image(key="frame", image=_rgb_array(), step=3, tags={"split": "val"})

    artifact, target = img.to_artifact(tmp_path, run_id=1, experiment_id=2)

    assert target.exists()
    assert target.suffix == ".jpg"
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
