"""Tests for the rle implementation."""

import numpy as np
import pytest

from dlboard.client.artifacts import rle
from dlboard.client.artifacts.overlay import ImageMask


@pytest.mark.parametrize(
    ("mask", "expected"),
    [
        pytest.param(np.zeros((1, 1, 1, 4), dtype=np.int64), [4, 0], id="one-run"),
        pytest.param(np.array([[[[1, 1, 2, 2, 2, 0]]]]), [2, 1, 3, 2, 1, 0], id="several-runs"),
        pytest.param(np.array([[[[3, 1, 3]]]]), [1, 3, 1, 1, 1, 3], id="alternating"),
    ],
)
def test_rle_pairs_count_and_id(mask: np.ndarray, expected: list[int]) -> None:
    assert rle.run_length_encode(mask).data == expected


def test_rle_round_trips_random_masks() -> None:
    rng = np.random.default_rng(seed=0)
    arr = rng.integers(0, 4, (1, 1, 20, 10))

    ret = rle.run_length_encode(arr, colors={}, classnames={})

    assert ret.encoding == "rle"
    assert np.array_equal(rle.run_length_decode(ret), arr)


@pytest.mark.parametrize(
    ("data", "shape", "expected"),
    [
        pytest.param([4, 0], (1, 1, 1, 4), np.zeros((1, 1, 1, 4)), id="one-run"),
        pytest.param([2, 1, 3, 2, 1, 0], (1, 1, 1, 6), np.array([[[[1, 1, 2, 2, 2, 0]]]]), id="several-runs"),
    ],
)
def test_rle_decode_expands_pairs_into_shape(
    data: list[int], shape: tuple[int, ...], expected: np.ndarray
) -> None:
    mask = ImageMask(encoding="rle", shape=list(shape), data=data, colors={}, classnames={})

    assert np.array_equal(rle.run_length_decode(mask), expected)


def test_rle_render_draws_colored_ids_and_leaves_others_transparent() -> None:
    mask = rle.run_length_encode(np.array([[[[0, 1, 2]]]]), colors={1: "#ff0000"}, classnames={})

    rendered = rle.run_length_render(mask)

    expected = np.array([[0, 0, 0, 0], [255, 0, 0, 255], [0, 0, 0, 0]], dtype=np.uint8).reshape(1, 1, 1, 3, 4)
    assert rendered.dtype == np.uint8
    assert np.array_equal(rendered, expected)
