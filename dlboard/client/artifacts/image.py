"""Log an image at a step."""

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal, cast

import dltype
import numpy as np
import PIL
import PIL.Image
from pydantic import BaseModel, field_validator

from dlboard.models import NewArtifact, TagValue, format_tags

if TYPE_CHECKING:
    # Pyright always sees the real `torch.Tensor` -- this repo's own dev/CI env always has torch
    # installed. The try/except below is the runtime-only fallback, for a `dlboard[torch]`-less
    # client install that never actually constructs an `Image` from a tensor.
    import torch

    _ImageArray = torch.Tensor | np.ndarray
else:
    try:
        import torch

        _ImageArray = torch.Tensor | np.ndarray
    except ImportError:
        # No placeholder type standing in for `torch.Tensor` here -- a client without torch
        # literally cannot hold one, so narrowing the field to `np.ndarray` alone is both more
        # accurate and sidesteps whatever pydantic/dltype's schema generation might do with an
        # annotation branch that's a fake, fieldless, non-array-like class.
        torch = None
        _ImageArray = np.ndarray


DEFAULT_FORMAT: Final = "png"
ImageFormat = Literal["png", "jpg"]


class Image(BaseModel, frozen=True, extra="forbid"):
    """An image to log to the backend."""

    key: str
    """Key for this artifact."""
    image: Annotated[_ImageArray, dltype.UInt8Tensor("height width *channels")]
    """An image to log. Expected to be in CHW format."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the image, for use by plugins."""
    step: int
    """The global step of the trainer."""
    format: ImageFormat = DEFAULT_FORMAT
    """How the image is encoded for upload: lossless `"png"` (the default), or `"jpg"` -- lossy, but a good
    deal smaller for large photographic images. Anything that must stay pixel-exact (masks, label images,
    annotated overlays) wants PNG."""

    @field_validator("image", mode="before")
    @classmethod
    def _move_image_to_cpu(cls, image: _ImageArray) -> _ImageArray:
        if torch is not None and isinstance(image, torch.Tensor):
            return image.cpu()
        return image

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        if torch is not None and isinstance(self.image, torch.Tensor):
            im_underlying = self.image.cpu().numpy()
        else:
            # Not a `torch.Tensor` (checked above whenever torch is even installed), so `_ImageArray`
            # leaves only `np.ndarray` here -- pyright just can't follow that through the compound
            # `torch is not None and ...` above.
            im_underlying = cast("np.ndarray", self.image)
        pil_img = PIL.Image.fromarray(im_underlying)
        target = (local_temp / str(uuid.uuid4())).with_suffix(f".{self.format}")
        pil_img.save(target)

        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=format_tags(self.tags),
        )
        return obj, target
