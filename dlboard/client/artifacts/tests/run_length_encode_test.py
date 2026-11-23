"""Tests for the rle implementation."""

from itertools import pairwise

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from pydantic import ValidationError

from dlboard.client.artifacts import rle
from dlboard.client.artifacts.overlay import ImageMask

# Few ids, so runs are long and often cross a row's end; a height or width of 1 is its own edge.
_MASKS = hnp.arrays(
    np.int64, hnp.array_shapes(min_dims=2, max_dims=2, max_side=6), elements=st.integers(0, 2)
)


@given(_MASKS)
def test_a_mask_is_its_runs_of_one_id_each_in_reading_order(mask: np.ndarray) -> None:
    """`N, I` pairs: a run of `N` pixels of id `I`, left to right and on into the next row, each as long as it goes."""
    encoded = rle.run_length_encode(mask)
    counts, ids = encoded.data[::2], encoded.data[1::2]

    assert encoded.shape == mask.shape
    assert np.array_equal(rle.run_length_decode(encoded), mask)
    assert all(count > 0 for count in counts)
    assert all(before != after for before, after in pairwise(ids))


@given(st.lists(st.integers(0, 3), max_size=4).map(tuple))
def test_a_mask_is_the_height_and_width_of_one_picture(shape: tuple[int, ...]) -> None:
    """Not a row (no width), a batch or a class axis, nor an empty picture."""
    one_picture = len(shape) == 2 and min(shape) > 0  # a height and a width

    if one_picture:
        assert ImageMask(shape=shape, data=[shape[0] * shape[1], 0]).shape == shape
    else:
        with pytest.raises(ValidationError):
            ImageMask(shape=shape, data=[4, 0])


def test_an_encoder_is_given_one_picture() -> None:
    with pytest.raises(Exception, match="ndims=2"):  # dltype's own error, which is not a ValidationError
        rle.run_length_encode(np.zeros((1, 2, 2), dtype=np.int64))
