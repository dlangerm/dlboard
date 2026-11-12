"""
Log a Plotly figure, drawn in the browser as the interactive figure it is.

Three ways to use it, all charted by the `plotly` chart:

- A `PlotlyFigure` at each step (a PR curve per epoch): the chart scrubs through the steps.
- One `PlotlyFigure` (step 0): the chart shows it as it is.
- One `PlotlyFigure` with `bind`, a template, then a `PlotlyData` at each step: the chart draws the template with the data
  logged so far in the traces it binds (a heatmap's `z`, a trajectory's `lat` and `lon`), and keeps drawing as more comes.

Nothing here imports plotly: a figure is anything with plotly's `to_json`, or a plain mapping of the same shape.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol, cast

import numpy as np
from pydantic import BaseModel, Field, NonNegativeInt, StringConstraints, field_validator, model_validator

from dlboard.client._arrays import ArrayLike, TensorLike, to_numpy
from dlboard.models import (
    PLOTLY_DATA_EXTENSION,
    PLOTLY_FIGURE_EXTENSION,
    NewArtifact,
    TagValue,
    plotly_data_key,
)

if TYPE_CHECKING:
    from pathlib import Path

BINDINGS_MEMBER: Final = "dlboard_bindings"
"""The member of a template's JSON its bindings are kept in, beside plotly's own `data` and `layout`."""

MAX_DATA_VALUES: Final = 1_000_000
"""The most values one `PlotlyData` may hold, over all its columns: a browser fetches and draws each one whole."""

MAX_DATA_DIMENSIONS: Final = 2


class ToPlotlyJson(Protocol):
    """Anything with plotly's `Figure.to_json` -- so the client never imports plotly."""

    def to_json(self) -> str:
        """The figure as plotly's JSON: its `data` (the traces), `layout` and so on."""
        ...


AttributePath = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")]
"""A trace attribute, dotted the way plotly names a nested one: `x`, `z`, `marker.color`."""

ColumnName = Annotated[str, StringConstraints(min_length=1)]

Bindings = Mapping[int, Mapping[str, str]]
"""For each trace, by its position in the figure's `data`, which attribute is filled from which `PlotlyData` column."""


class _Figure(BaseModel, frozen=True, extra="allow"):
    """What a figure must be for a chart to draw it: plotly's JSON, with its traces, and the bindings of a template."""

    data: list[dict[str, Any]]
    dlboard_bindings: dict[
        NonNegativeInt, Annotated[dict[AttributePath, ColumnName], Field(min_length=1)]
    ] = {}

    @model_validator(mode="after")
    def _bound_traces_exist(self) -> _Figure:
        if missing := sorted(trace for trace in self.dlboard_bindings if trace >= len(self.data)):
            msg = f"bindings name traces {missing}, but the figure has {len(self.data)}"
            raise ValueError(msg)
        return self


def _array_values(array: np.ndarray) -> list[Any]:
    """`array` as nested lists of plain values, with NaN and infinities as `None` (JSON's `null`, a gap to plotly)."""
    if array.dtype.kind == "f":
        values = array.astype(object)
        values[~np.isfinite(array)] = None
        return values.tolist()
    return _jsonable(array.tolist())


def _jsonable(value: object) -> Any:  # noqa: ANN401 -- whatever JSON value `value` is
    """`value` with every array, tensor and numpy scalar in it as plain values, and NaN as `None`."""
    match value:
        case str() | bool() | int() | None:
            return value
        case float():
            return value if math.isfinite(value) else None
        case Mapping():
            mapping = cast("Mapping[object, object]", value)
            return {str(key): _jsonable(item) for key, item in mapping.items()}
        case list() | tuple():
            return [_jsonable(item) for item in cast("Sequence[object]", value)]
        case np.generic():
            return _jsonable(value.item())
        case _ if isinstance(value, TensorLike):
            return _array_values(to_numpy(value))
        case _:
            msg = f"a figure cannot hold a {type(value).__name__}"
            raise TypeError(msg)


def _encoded(document: object) -> bytes:
    return json.dumps(document, allow_nan=False, separators=(",", ":")).encode()


class PlotlyFigure(BaseModel, frozen=True, extra="forbid"):
    """A Plotly figure, as plotly's JSON, to log to the backend; a template if it binds traces to data."""

    key: str
    """Key for this artifact."""
    data: bytes
    """The figure, as plotly's JSON."""
    step: int = 0
    """The global step of the trainer. Leave it at 0 for a figure logged once."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the figure, for use by plugins."""

    @classmethod
    def from_figure(
        cls,
        figure: ToPlotlyJson | Mapping[str, Any],
        key: str,
        step: int = 0,
        *,
        bind: Bindings | None = None,
        tags: Mapping[str, TagValue] | None = None,
    ) -> PlotlyFigure:
        """
        Encode `figure` right now -- it's mutable, and may be changed straight after logging.

        `bind` makes it a template: for each trace (by its position in `figure.data`), which attribute is filled from
        which column of the `PlotlyData` logged for `key` (`{0: {"lat": "lat", "lon": "lon"}}`). The rest of the figure
        (its layout, the traces' styles, any trace not bound) is drawn as it is.
        """
        raw = _jsonable(figure) if isinstance(figure, Mapping) else _jsonable(json.loads(figure.to_json()))
        if bind is not None:
            raw[BINDINGS_MEMBER] = bind
        checked = _Figure.model_validate(raw)
        if checked.dlboard_bindings:
            raw[BINDINGS_MEMBER] = checked.model_dump(mode="json")[BINDINGS_MEMBER]
        return cls(key=key, data=_encoded(raw), step=step, tags=tags or {})

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        target = local_temp / f"{uuid.uuid4()}{PLOTLY_FIGURE_EXTENSION}"
        target.write_bytes(self.data)
        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return obj, target


def _column(name: str, values: ArrayLike | Sequence[str] | float) -> np.ndarray:
    array = np.atleast_1d(to_numpy(values))  # pyrefly: ignore [bad-argument-type]
    if array.ndim > MAX_DATA_DIMENSIONS:
        msg = f"column {name!r} is {array.ndim}-D: a column is a list of values, or a 2-D array of rows"
        raise ValueError(msg)
    return array


class PlotlyData(BaseModel, frozen=True, extra="forbid"):
    """
    One step's data for the Plotly template logged under `template`, a column of values for each name its bindings use.

    A chart draws every step's data in step order: a 1-D column is appended to the one before (the points of a
    trajectory), and a 2-D one is stacked under it, row by row (a waterfall; log a single row as shape `(1, n)`). A
    chart can draw only the latest step's instead (a confusion matrix as it is now), or one step at a time.
    """

    template: str
    """The key of the template this data is drawn into."""
    step: int
    """The global step of the trainer."""
    columns: Annotated[Mapping[str, list[Any]], Field(min_length=1)]
    """Each column's values: a number, a list, an array or tensor of one or two dimensions, or a list of strings."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the data, for use by plugins."""

    @field_validator("columns", mode="before")
    @classmethod
    def _as_plain_columns(
        cls, columns: Mapping[str, ArrayLike | Sequence[str] | float]
    ) -> dict[str, list[Any]]:
        arrays = {name: _column(name, values) for name, values in columns.items()}
        if (size := sum(array.size for array in arrays.values())) > MAX_DATA_VALUES:
            msg = f"{size} values in one step's data, more than the {MAX_DATA_VALUES} a chart draws at once"
            raise ValueError(msg)
        return {name: _array_values(array) for name, array in arrays.items()}

    @property
    def key(self) -> str:
        """What the data is logged under: after its template's key (`plotly_data_key`), where the chart finds it."""
        return plotly_data_key(self.template)

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        target = local_temp / f"{uuid.uuid4()}{PLOTLY_DATA_EXTENSION}"
        target.write_bytes(_encoded({"columns": dict(self.columns)}))
        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=dict(self.tags),
        )
        return obj, target
