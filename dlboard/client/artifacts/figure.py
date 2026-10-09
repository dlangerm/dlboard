"""Log a matplotlib figure, rendered to an image file (PNG by default), at a step."""

from __future__ import annotations

import io
import uuid
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, get_args

from pydantic import BaseModel

from dlboard.models import NewArtifact, TagValue, format_tags

if TYPE_CHECKING:
    from pathlib import Path


class SavesFigure(Protocol):
    """Anything with matplotlib's `Figure.savefig` signature -- so the client never imports matplotlib."""

    def savefig(self, fname: Any, /, **kwargs: Any) -> None:  # noqa: ANN401
        """Render this figure into `fname`."""
        ...


DEFAULT_FORMAT: Final = "png"
FigureFormat = Literal["png", "svg", "jpg", "jpeg"]
_SUPPORTED_FORMATS: Final = frozenset(get_args(FigureFormat))


class Figure(BaseModel, frozen=True, extra="forbid"):
    """A figure, already rendered to bytes (PNG unless asked otherwise), to log to the backend."""

    key: str
    """Key for this artifact."""
    data: bytes
    """The rendered figure, encoded as `format`."""
    tags: Mapping[str, TagValue] = {}
    """Tags for the figure, for use by plugins."""
    step: int
    """The global step of the trainer."""
    format: FigureFormat = DEFAULT_FORMAT
    """What `data` is encoded as; it's the artifact's file extension."""

    @classmethod
    def from_figure(
        cls,
        figure: SavesFigure,
        key: str,
        step: int,
        *,
        tags: Mapping[str, TagValue] | None = None,
        save_kwargs: dict[str, Any] | None = None,
    ) -> Figure:
        """
        Render `figure` right now -- it's mutable, and callers usually close it right after logging.

        `save_kwargs` goes to `savefig` (`dpi`, `bbox_inches`, ...) like mlflow's. The format is
        `save_kwargs["format"]` if given, else `key`'s extension when it's a supported one (so an
        mlflow-style `"loss.svg"` just works), else PNG. An unsupported explicit format is rejected.
        """
        extension = PurePosixPath(key).suffix.removeprefix(".").lower()
        format_key = "format"
        kwargs = {format_key: extension if extension in _SUPPORTED_FORMATS else DEFAULT_FORMAT} | (
            save_kwargs or {}
        )
        buf = io.BytesIO()
        figure.savefig(buf, **kwargs)
        return cls(
            key=key,
            data=buf.getvalue(),
            step=step,
            tags=tags or {},
            format=kwargs[format_key],  # pyright: ignore[reportArgumentType]
        )

    def to_artifact(self, local_temp: Path, run_id: int, experiment_id: int) -> tuple[NewArtifact, Path]:
        """Convert this artifact."""
        target = (local_temp / str(uuid.uuid4())).with_suffix(f".{self.format}")
        target.write_bytes(self.data)
        obj = NewArtifact(
            key=self.key,
            fname=target.name,
            run_id=run_id,
            experiment_id=experiment_id,
            step=self.step,
            tags=format_tags(self.tags),
        )
        return obj, target
