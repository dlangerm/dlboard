"""Log an image at a step."""

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal

import dltype
import PIL
import PIL.Image
from pydantic import BaseModel, field_validator

from dlboard.client._arrays import TensorLike, to_host, to_numpy
from dlboard.models import NewArtifact, TagValue

DEFAULT_FORMAT: Final = "png"
ImageFormat = Literal["png", "jpg"]


class Image(BaseModel, frozen=True, extra="forbid"):
    """An image to log to the backend."""

    key: str
    """Key for this artifact."""
    image: Annotated[TensorLike, dltype.UInt8Tensor("height width *channels")]
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
    def _move_image_to_cpu(cls, image: TensorLike) -> TensorLike:
        return to_host(image)

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        pil_img = PIL.Image.fromarray(to_numpy(self.image))
        target = (local_temp / str(uuid.uuid4())).with_suffix(f".{self.format}")
        pil_img.save(target)

        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return obj, target
