"""An overlay is logged as a JSON file of its shapes, at its own key and step, and rejects malformed shapes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from dlboard.client.artifacts.overlay import Box, Overlay, Point, Polygon
from dlboard.models import OVERLAY_KEY_SUFFIX

if TYPE_CHECKING:
    from pathlib import Path


def test_shapes_are_written_as_json_at_the_overlays_key_and_step(tmp_path: Path) -> None:
    overlay = Overlay(
        key="samples/boxes",
        step=3,
        shapes=[
            Box(x=0.1, y=0.2, width=0.3, height=0.4, label="cat"),
            Polygon(points=[Point(0, 0), Point(1, 0), Point(0, 1)]),
        ],
    )

    artifact, target = overlay.to_artifact(tmp_path, run_id=5, experiment_id=6)

    assert (artifact.key, artifact.step, artifact.run_id, artifact.experiment_id) == (
        "samples/boxes",
        3,
        5,
        6,
    )
    assert target.suffix == ".json"
    shapes: list[dict[str, Any]] = json.loads(target.read_text())["shapes"]
    assert shapes[0] == {
        "kind": "box",
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.4,
        "label": "cat",
        "color": "#ff3b30",
    }
    assert shapes[1]["kind"] == "polygon"
    polygon = overlay.shapes[1]
    assert isinstance(polygon, Polygon)
    assert polygon.points == [Point(0, 0), Point(1, 0), Point(0, 1)]
    assert shapes[1]["points"] == [
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
    ]  # a point is stored as its x, then its y


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(
            {"kind": "box", "x": 0, "y": 0, "width": 1, "height": 1, "color": "red"}, id="color-not-hex"
        ),
        pytest.param({"kind": "box", "x": 0, "y": 0, "width": -0.1, "height": 1}, id="negative-width"),
        pytest.param(
            {"kind": "box", "x": 12, "y": 0, "width": 0.1, "height": 0.1}, id="pixels-not-a-fraction"
        ),
        pytest.param({"kind": "polygon", "points": [[0, 0], [1, 1], [2, 0]]}, id="point-past-the-edge"),
        pytest.param(
            {"kind": "box", "x": 0, "y": 0, "width": 1, "height": 1, "label": "x" * 1001}, id="label-too-long"
        ),
        pytest.param({"kind": "polygon", "points": [[0, 0], [1, 1]]}, id="polygon-under-three-points"),
        pytest.param({"kind": "circle", "x": 0, "y": 0, "r": 1}, id="unknown-kind"),
    ],
)
def test_a_malformed_shape_is_rejected(shape: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Overlay.model_validate({"key": "samples/boxes", "step": 0, "shapes": [shape]})


def test_an_overlay_over_an_image_is_logged_under_the_key_an_auto_generated_chart_looks_for(
    tmp_path: Path,
) -> None:
    overlay = Overlay.over("samples/a", shapes=[Box(x=0, y=0, width=1, height=1)], step=4, tags={"n": 1})

    artifact, _ = overlay.to_artifact(tmp_path, run_id=1, experiment_id=1)

    assert (artifact.key, artifact.step, artifact.tags) == (f"samples/a{OVERLAY_KEY_SUFFIX}", 4, {"n": 1})
