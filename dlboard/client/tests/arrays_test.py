"""Tests for turning whatever a training loop holds into a host numpy array."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest
import torch

from dlboard.client._arrays import to_host, to_numpy

if TYPE_CHECKING:
    from dlboard.client._arrays import ArrayLike


@pytest.mark.parametrize(
    ("values", "dtype"),
    [
        ([1.0, 2.0], np.float64),
        (np.array([1, 2], dtype=np.int16), np.int16),
        (torch.tensor([1.0, 2.0], requires_grad=True) * 1, np.float32),
        (torch.tensor([1.0, 2.0], dtype=torch.bfloat16), np.float32),
        (torch.tensor([1.0, 2.0], dtype=torch.float16), np.float16),
    ],
)
def test_anything_array_like_becomes_a_numpy_array_with_the_same_values(
    values: ArrayLike, dtype: type[np.generic]
) -> None:
    array = to_numpy(values)

    assert array.dtype == dtype
    assert array.tolist() == [1, 2]


def test_a_tensor_comes_off_the_autograd_graph_but_stays_a_tensor() -> None:
    tensor = torch.ones(2, requires_grad=True) * 2

    host = to_host(tensor)

    assert isinstance(host, torch.Tensor)
    assert not host.requires_grad
