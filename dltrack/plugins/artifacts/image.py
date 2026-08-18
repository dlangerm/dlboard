"""Log an image at a step."""

import uuid
from pathlib import Path
from typing import Annotated

import dltype
import numpy as np
import PIL
import PIL.Image
import torch
from pydantic import BaseModel, field_validator

from dltrack.models._artifact import NewArtifact


class Image(BaseModel, frozen=True, extra="forbid"):
    """An image to log to the backend."""

    key: str
    """Key for this artifact."""
    image: Annotated[torch.Tensor | np.ndarray, dltype.UInt8Tensor("height width *channels")]
    """An image to log. Expected to be in CHW format."""
    tags: dict[str, str] = {}
    """Tags for the image, for use by plugins."""
    step: int
    """The global step of the trainer."""

    @field_validator("image", mode="before")
    @classmethod
    def _move_image_to_cpu(cls, image: torch.Tensor | np.ndarray) -> torch.Tensor | np.ndarray:
        if isinstance(image, torch.Tensor):
            return image.cpu()
        return image

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        im_underlying = self.image.numpy() if isinstance(self.image, torch.Tensor) else self.image
        pil_img = PIL.Image.fromarray(im_underlying)
        target = (local_temp / str(uuid.uuid4())).with_suffix(".jpg")
        pil_img.save(target)

        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=self.tags,
        )
        return obj, target
