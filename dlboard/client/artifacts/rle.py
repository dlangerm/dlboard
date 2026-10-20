"""Run length encode an image mask."""

from typing import Annotated

import dltype
import numpy as np

from dlboard.client.artifacts.overlay import HexColor, ImageMask


@dltype.dltyped()
def _rle(arr: Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]]) -> list[int]:
    """Flatten the mask in C order and emit alternating `N, I` pairs: a run of `N` elements all of ID `I`."""
    _flat_arr: np.ndarray = np.ravel(arr)
    if _flat_arr.size == 0:
        return []

    # vectorized: a run starts wherever an element differs from the one before it
    _run_starts = np.concatenate(([0], np.flatnonzero(_flat_arr[1:] != _flat_arr[:-1]) + 1))
    _run_lengths = np.diff(np.append(_run_starts, _flat_arr.size))
    _run_ids = _flat_arr[_run_starts]

    return np.column_stack((_run_lengths, _run_ids)).ravel().tolist()


@dltype.dltyped()
def run_length_encode(
    segmentation_mask: Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]],
    colors: dict[int, HexColor] | None = None,
    classnames: dict[int, str] | None = None,
) -> ImageMask:
    """Rle encode an image mask."""
    return ImageMask(
        kind="mask",
        shape=list(segmentation_mask.shape),
        data=_rle(segmentation_mask),
        encoding="rle",
        colors=colors,
        classnames=classnames,
    )


@dltype.dltyped()
def _rle_decode(
    data: list[int], shape: tuple[int, ...]
) -> Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]]:
    """Expand alternating `N, I` pairs back into `N` elements of ID `I` each, reshaped to `shape` in C order."""
    _pairs = np.asarray(data, dtype=np.int64).reshape(-1, 2)
    return np.repeat(_pairs[:, 1], _pairs[:, 0]).reshape(shape)


@dltype.dltyped()
def run_length_decode(
    mask: ImageMask,
) -> Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]]:
    """Decode an rle `ImageMask` back to an integer array of the shape it was encoded from."""
    if mask.encoding != "rle":
        msg = f"Invalid encoding {mask.encoding} expected rle"
        raise ValueError(msg)
    return _rle_decode(mask.data, tuple(mask.shape))


@dltype.dltyped()
def run_length_render(
    mask: ImageMask,
) -> Annotated[np.ndarray, dltype.UInt8Tensor["*batch classes height width channels"]]:
    """
    Draw an rle `ImageMask` as RGBA pixels, one per element of its shape.

    An ID with a color in `colors` is drawn opaque in that color; an ID without one is transparent, so a background
    class (usually `0`) stays out of the picture it is laid over.
    """
    ids = run_length_decode(mask)
    rgba = np.zeros((*ids.shape, 4), dtype=np.uint8)
    for class_id, color in (mask.colors or {}).items():
        rgba[ids == class_id] = (*bytes.fromhex(color[1:]), 255)
    return rgba
