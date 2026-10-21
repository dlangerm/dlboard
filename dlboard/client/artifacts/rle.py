"""Run length encode an image mask."""

from typing import Annotated

import dltype
import numpy as np

from dlboard.client.artifacts.overlay import DEFAULT_MASK_OPACITY, HexColor, ImageMask


@dltype.dltyped()
def _rle(arr: Annotated[np.ndarray, dltype.IntTensor["height width"]]) -> list[int]:
    """Flatten the mask in C order and emit alternating `N, I` pairs: a run of `N` pixels all of ID `I`."""
    flat: np.ndarray = np.ravel(arr)
    if flat.size == 0:
        return []

    # vectorized: a run starts wherever an element differs from the one before it
    starts = np.concatenate(([0], np.flatnonzero(flat[1:] != flat[:-1]) + 1))
    lengths = np.diff(np.append(starts, flat.size))
    ids = flat[starts]

    return np.column_stack((lengths, ids)).ravel().tolist()


@dltype.dltyped()
def run_length_encode(
    segmentation_mask: Annotated[np.ndarray, dltype.IntTensor["height width"]],
    colors: dict[int, HexColor] | None = None,
    classnames: dict[int, str] | None = None,
    opacity: float = DEFAULT_MASK_OPACITY,
) -> ImageMask:
    """
    Rle encode the class ID of each pixel of a picture (`height width`), drawn at `opacity` (see `ImageMask.opacity`).

    One picture's mask: take one out of a batch, and argmax a one-hot `classes height width` over its classes first.
    """
    height, width = segmentation_mask.shape
    return ImageMask(
        kind="mask",
        shape=(height, width),
        data=_rle(segmentation_mask),
        colors=colors,
        classnames=classnames,
        opacity=opacity,
    )


@dltype.dltyped()
def _rle_decode(
    data: list[int], shape: tuple[int, int]
) -> Annotated[np.ndarray, dltype.IntTensor["height width"]]:
    """Expand alternating `N, I` pairs back into `N` pixels of ID `I` each, reshaped to `shape` in C order."""
    _pairs = np.asarray(data, dtype=np.int64).reshape(-1, 2)
    return np.repeat(_pairs[:, 1], _pairs[:, 0]).reshape(shape)


@dltype.dltyped()
def run_length_decode(mask: ImageMask) -> Annotated[np.ndarray, dltype.IntTensor["height width"]]:
    """Decode an rle `ImageMask` back to the integer array of the shape it was encoded from."""
    match mask.encoding:
        case "rle":
            return _rle_decode(mask.data, mask.shape)
