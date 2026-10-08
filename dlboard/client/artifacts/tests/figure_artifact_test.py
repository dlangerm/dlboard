# pyright: reportPrivateUsage=false
"""Tests for the Figure artifact's matplotlib figure -> image file conversion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import matplotlib.figure
import PIL.Image
import pydantic
import pytest

from dlboard.client.artifacts.figure import Figure

if TYPE_CHECKING:
    from pathlib import Path


def _figure() -> matplotlib.figure.Figure:
    fig = matplotlib.figure.Figure(figsize=(2, 1))
    fig.subplots().plot([1, 2, 3])  # pyright: ignore[reportUnknownMemberType]
    return fig


def test_to_artifact_writes_the_image_with_the_figures_metadata(tmp_path: Path) -> None:
    figure = Figure.from_figure(_figure(), "curve", step=3, tags={"split": "val"}, save_kwargs={"dpi": 100})

    artifact, target = figure.to_artifact(tmp_path, run_id=1, experiment_id=2)

    with PIL.Image.open(target) as written:
        assert (written.format, written.size) == ("PNG", (200, 100))
    assert target.suffix == ".png"
    assert artifact.key == "curve"
    assert artifact.fname == target.name
    assert artifact.run_id == 1
    assert artifact.experiment_id == 2
    assert artifact.step == 3
    assert artifact.tags == {"split": "val"}


@pytest.mark.parametrize(
    ("key", "save_kwargs", "expected"),
    [
        ("val/confusion_matrix", None, "png"),
        ("loss.png", None, "png"),
        ("loss.jpg", None, "jpg"),
        ("loss.JPEG", None, "jpeg"),
        ("loss.svg", None, "svg"),
        ("run.v2/loss", None, "png"),
        ("loss.pdf", None, "png"),
        ("loss.png", {"format": "svg"}, "svg"),
    ],
    ids=[
        "no-extension",
        "png",
        "jpg",
        "uppercase-jpeg",
        "svg",
        "dot-in-a-directory",
        "unsupported",
        "explicit",
    ],
)
def test_the_format_comes_from_the_key_extension_else_png(
    tmp_path: Path, key: str, save_kwargs: dict[str, Any] | None, expected: str
) -> None:
    figure = Figure.from_figure(_figure(), key, step=0, save_kwargs=save_kwargs)

    _artifact, target = figure.to_artifact(tmp_path, run_id=1, experiment_id=1)

    assert figure.format == expected
    assert target.suffix == f".{expected}"
    if expected == "svg":
        assert b"<svg" in figure.data
    else:
        with PIL.Image.open(target) as written:
            assert written.format == expected.upper().replace("JPG", "JPEG")


def test_an_unsupported_explicit_format_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        Figure.from_figure(_figure(), "loss", step=0, save_kwargs={"format": "pdf"})
