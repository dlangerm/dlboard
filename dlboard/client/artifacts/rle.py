"""Run length encode an image mask."""

from typing import Annotated

import dltype
import numpy as np

from dlboard.client.artifacts.overlay import DEFAULT_MASK_OPACITY, HexColor, ImageMask


@dltype.dltyped()
def _rle(arr: Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]]) -> list[int]:
    """Flatten the mask in C order and emit alternating `N, I` pairs: a run of `N` elements all of ID `I`."""
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
    segmentation_mask: Annotated[np.ndarray, dltype.IntTensor["*batch classes height width"]],
    colors: dict[int, HexColor] | None = None,
    classnames: dict[int, str] | None = None,
    opacity: float = DEFAULT_MASK_OPACITY,
) -> ImageMask:
    """Rle encode an image mask, drawn at `opacity` (see `ImageMask.opacity`)."""
    return ImageMask(
        kind="mask",
        shape=list(segmentation_mask.shape),
        data=_rle(segmentation_mask),
        colors=colors,
        classnames=classnames,
        opacity=opacity,
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
    match mask.encoding:
        case "rle":
            return _rle_decode(mask.data, tuple(mask.shape))


@dltype.dltyped()
def run_length_render(
    mask: ImageMask,
) -> Annotated[np.ndarray, dltype.UInt8Tensor["*batch classes height width channels"]]:
    """
    Draw an rle `ImageMask` as RGBA pixels, one per element of its shape.

    An ID with a color (`ImageMask.class_color`) is drawn in it at the mask's opacity; an ID without one is transparent,
    so a background class (usually `0`) stays out of the picture it is laid over. The browser draws it the same way.

    This is the reference for what the browser draws (`image_series_overlay.js`'s `maskPicture`), and is what the
    Python tests check that drawing rule against. Nothing on the server calls it.
    """
    ids: np.ndarray = run_length_decode(mask)
    rgba = np.zeros((*ids.shape, 4), dtype=np.uint8)
    alpha = int(mask.opacity * 255 + 0.5)
    for class_id in map(int, np.unique(ids)):
        if (color := mask.class_color(class_id)) is not None:
            rgba[ids == class_id] = (*bytes.fromhex(color[1:]), alpha)
    return rgba
