"""Log the shapes drawn over an image, as a file paired with that image at the same step."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Final, Literal, NamedTuple

from pydantic import BaseModel, Field, StringConstraints

from dlboard.models import OVERLAY_KEY_SUFFIX, NewArtifact, TagValue

if TYPE_CHECKING:
    from pathlib import Path

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
"""A color as `#rrggbb`."""

Fraction = Annotated[float, Field(ge=0, le=1)]
"""
A share of the picture's width or height, from 0 at its left or top edge to 1 at its right or bottom edge.

Shapes are stored as fractions, not pixels, so they can be drawn at any size and need no knowledge of the
picture's own: divide a pixel coordinate by the picture's width (for `x`) or height (for `y`).
"""

DEFAULT_SHAPE_COLOR: Final = "#ff3b30"
MAX_OVERLAY_SHAPES: Final = 1000
MAX_SHAPE_LABEL_LENGTH: Final = 1000
"""Long enough for a custom chart to keep more than a name in a label (a line of text, a small JSON document)."""


class Point(NamedTuple):
    """
    A position in the picture, as `Fraction`s of its size.

    The origin is the picture's top-left corner, with `x` growing to the right and `y` growing *down*: the way
    image libraries (OpenCV, PIL, numpy) index pixels, not the way a plot's axes run.
    """

    x: Fraction
    y: Fraction


class Box(BaseModel, frozen=True, extra="forbid"):
    """An axis-aligned rectangle, from its top-left corner, as `Fraction`s of the picture's size."""

    kind: Literal["box"] = "box"
    x: Fraction
    """Where the left edge is, as a share of the picture's width."""
    y: Fraction
    """Where the top edge is, as a share of the picture's height."""
    width: Fraction
    """How wide the box is, as a share of the picture's width."""
    height: Fraction
    """How tall the box is, as a share of the picture's height."""
    label: str | None = Field(default=None, max_length=MAX_SHAPE_LABEL_LENGTH)
    """Text drawn at the box's top-left corner, if any."""
    color: HexColor = DEFAULT_SHAPE_COLOR


class Polygon(BaseModel, frozen=True, extra="forbid"):
    """A closed outline through `points`, each a `Point` (`Fraction`s of the picture's size)."""

    kind: Literal["polygon"] = "polygon"
    points: list[Point] = Field(min_length=3)
    label: str | None = Field(default=None, max_length=MAX_SHAPE_LABEL_LENGTH)
    """Text drawn at the first point, if any."""
    color: HexColor = DEFAULT_SHAPE_COLOR


Shape = Annotated[Box | Polygon, Field(discriminator="kind")]
"""Any shape an overlay can hold."""


class Overlay(BaseModel, frozen=True, extra="forbid"):
    """
    Shapes drawn over a picture, logged as a file at the same step as the picture.

    The image chart pairs the two by run and step, and lays the shapes over the picture in the browser, so the
    picture itself stays untouched and can be a compact JPEG. Shapes are fractions of its size (`Fraction`, `Point`).
    Logged under `OVERLAY_KEY_SUFFIX` after the picture's key (`Overlay.over`), an auto-generated image chart draws
    it with no setup; under any other key, the chart's `overlays` setting names it.
    """

    key: str
    """Key for this artifact."""
    shapes: list[Shape] = Field(max_length=MAX_OVERLAY_SHAPES)
    """The shapes to draw, at most `MAX_OVERLAY_SHAPES` of them."""
    step: int
    """The global step of the trainer; the picture it annotates is logged at the same step."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the overlay, for use by plugins."""

    @classmethod
    def over(
        cls, image_key: str, *, shapes: list[Shape], step: int, tags: Mapping[str, TagValue] | None = None
    ) -> Overlay:
        """The overlay of the image logged under `image_key`, under the key an auto-generated chart looks for."""
        return cls(key=f"{image_key}{OVERLAY_KEY_SUFFIX}", shapes=shapes, step=step, tags=tags or {})

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Write the shapes as JSON, and describe the file."""
        target = (local_temp / str(uuid.uuid4())).with_suffix(".json")
        target.write_text(self.model_dump_json(include={"shapes"}))

        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return obj, target
