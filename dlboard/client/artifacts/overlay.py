"""Log the shapes drawn over an image, as a file paired with that image at the same step."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Final, Literal, NamedTuple

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, StringConstraints, model_validator

from dlboard.models import LAYER_KIND_TAG, LayerKind, NewArtifact, TagValue, overlay_key

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
DEFAULT_CLASS_COLORS: Final[tuple[HexColor, ...]] = (
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#42d4f4",
    "#f032e6",
    "#bfef45",
    "#fabed4",
    "#469990",
)
"""
The colors a mask with no `colors` draws its classes in: class 1 takes the first, class 2 the second, and so on, around.

A palette of its own, not the run palette: a class drawn in run 1's color would read as run 1. `image_series_overlay.js`
holds the same list; a test keeps the two equal.
"""
DEFAULT_MASK_OPACITY: Final = 0.5
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


class Polyline(BaseModel, frozen=True, extra="forbid"):
    """An open line through `points`, each a `Point` (`Fraction`s of the picture's size)."""

    kind: Literal["polyline"] = "polyline"
    points: list[Point] = Field(min_length=2)
    label: str | None = Field(default=None, max_length=MAX_SHAPE_LABEL_LENGTH)
    """Text drawn at the first point, if any."""
    color: HexColor = DEFAULT_SHAPE_COLOR


class Polygon(Polyline):
    """A `Polyline` that is closed: the last point joins back to the first, so it outlines an area."""

    # A closed variant is a different kind in the file, for the discriminated union; a type checker sees a narrower literal.
    kind: Literal["polygon"] = "polygon"  # pyrefly: ignore [bad-override]
    points: list[Point] = Field(min_length=3)


class ImageMask(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """A mask drawn over a picture: each pixel's class ID, run-length encoded, and the color of each class."""

    kind: Literal["mask"] = "mask"
    encoding: Literal["rle"] = "rle"
    shape: tuple[PositiveInt, PositiveInt]
    """The picture's height and width, which the mask covers pixel for pixel in C order (row by row)."""
    data: list[NonNegativeInt]
    """List of alternating N,I where N is the number of elements and I is the ID common to them."""
    colors: dict[int, HexColor] | None = None
    """
    The color each class ID is drawn in. An ID with none is left see-through, so a background class stays clear.
    Leave this out for the default: ID 0 is clear and every other ID takes a color of `DEFAULT_CLASS_COLORS`.
    """
    classnames: dict[int, str] | None = None
    """The name of each class ID, for a legend."""
    opacity: Fraction = DEFAULT_MASK_OPACITY
    """How opaque the colored classes are, from 0 (invisible) to 1, so the picture shows through them."""

    @model_validator(mode="after")
    def _runs_cover_the_shape(self) -> ImageMask:
        if len(self.data) % 2 != 0:
            msg = "rle data is alternating N,I pairs, so it has an even length"
            raise ValueError(msg)
        elements = math.prod(self.shape)
        if sum(self.data[::2]) != elements:
            msg = f"rle runs cover {sum(self.data[::2])} elements, not the {elements} of shape {self.shape}"
            raise ValueError(msg)
        return self


Shape = Annotated[Box | Polyline | Polygon | ImageMask, Field(discriminator="kind")]
"""Any shape an overlay can hold."""


class Overlay(BaseModel, frozen=True, extra="forbid"):
    """
    Shapes drawn over a picture, logged as a file at the same step as the picture.

    The image chart pairs the two by run and step, and lays the shapes over the picture in the browser, so the
    picture itself stays untouched and can be a compact JPEG. Shapes are fractions of its size (`Fraction`, `Point`).
    Logged under `OVERLAY_KEY_SUFFIX` after the picture's key (`Overlay.over`), an auto-generated image chart draws
    it with no setup; under any other key, the chart's Layers table (`layers` setting) names it.
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
        cls,
        image_key: str,
        *,
        shapes: list[Shape],
        step: int,
        name: str | None = None,
        tags: Mapping[str, TagValue] | None = None,
    ) -> Overlay:
        """
        The overlay of the image logged under `image_key`, under the key an auto-generated chart looks for.

        An image can have several overlays (predicted and ground-truth boxes, say): give each its own `name`, since
        two logged under one key at one step would be saved to the same place. They are drawn in key order.
        """
        return cls(key=overlay_key(image_key, name), shapes=shapes, step=step, tags=tags or {})

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
            tags={**self.tags, LAYER_KIND_TAG: LayerKind.SHAPES.value},
        )
        return obj, target
