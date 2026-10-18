"""An artifact is drawn as a layer by the kind it says it is, else as a picture if it is an image."""

from __future__ import annotations

import pytest

from dlboard.models import LAYER_KIND_TAG, LayerKind, layer_kind


@pytest.mark.parametrize(
    ("tags", "fname", "kind"),
    [
        pytest.param({LAYER_KIND_TAG: "shapes"}, "boxes.json", LayerKind.SHAPES, id="the-tag-says-shapes"),
        pytest.param({LAYER_KIND_TAG: "shapes"}, "boxes.bin", LayerKind.SHAPES, id="not-the-extension"),
        pytest.param({LAYER_KIND_TAG: "picture"}, "mask.png", LayerKind.PICTURE, id="the-tag-says-picture"),
        pytest.param({}, "mask.png", LayerKind.PICTURE, id="an-image-with-no-tag-is-a-picture"),
        pytest.param({}, "model.ckpt", None, id="a-file-with-no-tag-is-no-layer"),
        pytest.param({LAYER_KIND_TAG: "rle-mask"}, "mask.npz", None, id="a-kind-this-server-lacks"),
    ],
)
def test_a_layers_kind_comes_from_its_tag_and_an_image_defaults_to_a_picture(
    tags: dict[str, str], fname: str, kind: LayerKind | None
) -> None:
    assert layer_kind(tags, fname) is kind
