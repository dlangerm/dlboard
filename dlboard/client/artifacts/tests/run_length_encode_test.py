"""Tests for the rle implementation."""

import numpy as np
import pytest

from dlboard.client.artifacts import rle
from dlboard.client.artifacts.overlay import DEFAULT_CLASS_COLORS, ImageMask


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
    rng = np.random.default_rng()
    arr = rng.integers(0, 4, (2, 3, 20, 10))

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


def _rgb(color: str) -> list[int]:
    return list(bytes.fromhex(color[1:]))


@pytest.mark.parametrize(
    ("colors", "opacity", "class_one", "class_two"),
    [
        pytest.param({1: "#ff0000"}, 1.0, [255, 0, 0, 255], [0, 0, 0, 0], id="only-colored-ids-are-drawn"),
        pytest.param({1: "#ff0000"}, 0.5, [255, 0, 0, 128], [0, 0, 0, 0], id="translucent"),
        pytest.param(
            None,
            1.0,
            [*_rgb(DEFAULT_CLASS_COLORS[0]), 255],
            [*_rgb(DEFAULT_CLASS_COLORS[1]), 255],
            id="default-palette",
        ),
    ],
)
def test_rle_render_draws_colored_ids_at_the_masks_opacity_and_leaves_others_transparent(
    colors: dict[int, str] | None, opacity: float, class_one: list[int], class_two: list[int]
) -> None:
    mask = rle.run_length_encode(np.array([[[[0, 1, 2]]]]), colors=colors).model_copy(
        update={"opacity": opacity}
    )

    rendered = rle.run_length_render(mask)

    assert rendered.dtype == np.uint8
    assert rendered.reshape(3, 4).tolist() == [[0, 0, 0, 0], class_one, class_two]
