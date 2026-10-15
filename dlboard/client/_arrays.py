"""
Treat a torch tensor and a numpy array alike, for a client install that may have no torch at all.

Whatever a client logs from a training loop (an image, the values of a histogram) may live on a GPU, hang off an
autograd graph, or be a dtype numpy has no name for. `to_numpy` is the one place that turns it into something
that can be put on a queue and handed to the shipping process.
"""

from __future__ import annotations

import typing

import numpy as np

if typing.TYPE_CHECKING:
    # Pyrefly always sees the real `torch.Tensor` -- this repo's own dev/CI env always has torch installed.
    # The try/except below is the runtime-only fallback, for a client install that has no torch at all.
    import torch

    TensorLike: typing.TypeAlias = torch.Tensor | np.ndarray
else:
    try:
        import torch

        TensorLike = torch.Tensor | np.ndarray
    except ImportError:
        # No placeholder standing in for `torch.Tensor`: a client without torch cannot hold one, so the
        # narrower type is the accurate one (and the only one pydantic's schema generation can make sense of).
        torch = None
        TensorLike = np.ndarray

ArrayLike: typing.TypeAlias = TensorLike | typing.Sequence[float]
"""Anything `to_numpy` accepts."""


def to_host(values: TensorLike) -> TensorLike:
    """`values` in host memory and off any autograd graph: a tensor is moved off its device, an array is as it is."""
    if torch is not None and isinstance(values, torch.Tensor):
        return values.detach().cpu()
    return values


def to_numpy(values: ArrayLike) -> np.ndarray:
    """
    `values` as a numpy array in host memory, whatever it was.

    A bfloat16 tensor comes out as float32, since numpy has no bfloat16. Nothing else is cast or copied
    needlessly: an array that is already one comes back as it is.
    """
    host = to_host(values) if isinstance(values, TensorLike) else values
    if torch is not None and isinstance(host, torch.Tensor):
        return (host.float() if host.dtype is torch.bfloat16 else host).numpy()
    return np.asarray(host)
