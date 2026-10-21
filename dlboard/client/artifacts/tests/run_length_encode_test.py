"""Tests for the rle implementation."""

import numpy as np
import pytest
from pydantic import ValidationError

from dlboard.client.artifacts import rle
from dlboard.client.artifacts.overlay import ImageMask


@pytest.mark.parametrize(
    ("mask", "expected"),
    [
        pytest.param(np.zeros((1, 4), dtype=np.int64), [4, 0], id="one-run"),
        pytest.param(np.array([[1, 1, 2, 2, 2, 0]]), [2, 1, 3, 2, 1, 0], id="several-runs"),
        pytest.param(np.array([[3, 1, 3]]), [1, 3, 1, 1, 1, 3], id="alternating"),
        pytest.param(np.array([[1, 1], [1, 2]]), [3, 1, 1, 2], id="a-run-wraps-onto-the-next-row"),
    ],
)
def test_rle_pairs_count_and_id(mask: np.ndarray, expected: list[int]) -> None:
    assert rle.run_length_encode(mask).data == expected


def test_rle_round_trips_random_masks() -> None:
    rng = np.random.default_rng(seed=0)
    arr = rng.integers(0, 4, (20, 10))

    ret = rle.run_length_encode(arr, colors={}, classnames={})

    assert ret.encoding == "rle"
    assert ret.shape == (20, 10)
    assert np.array_equal(rle.run_length_decode(ret), arr)


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param((4,), id="no-width"),
        pytest.param((1, 2, 2), id="a-batch-or-a-class-axis"),
        pytest.param((0, 4), id="an-empty-picture"),
    ],
)
def test_a_mask_is_the_height_and_width_of_one_picture(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        ImageMask(shape=shape, data=[4, 0])


def test_an_encoder_is_given_one_picture() -> None:
    with pytest.raises(Exception, match="ndims=2"):  # dltype's own error, which is not a ValidationError
        rle.run_length_encode(np.zeros((1, 2, 2), dtype=np.int64))
