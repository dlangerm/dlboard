"""
The layers drawn over a picture, for every chart that shows a picture with them (the image chart, the table chart).

A layer is an artifact logged beside the picture at the same step: an overlay of shapes (`Overlay`, logged under the
picture's key and `OVERLAY_KEY_SUFFIX`) or another image. A chart finds the ones a picture has (`drawn_layers`) and hands
them to the browser, which draws them (`_layered_picture.js`).
"""

from __future__ import annotations

import json
import typing
from pathlib import Path
from weakref import WeakSet

from pydantic import BaseModel

from dlboard.client.artifacts.overlay import DEFAULT_CLASS_COLORS, DEFAULT_MASK_OPACITY
from dlboard.models import (
    LayerKind,
    OverlayKey,
    artifact_column,
    artifact_fname_column,
    artifact_keys_in,
    artifact_tags_column,
    layer_kind,
    overlay_key,
)
from dlboard.plugins.charts._artifact_tags import parse_tags
from dlboard.serve import AssetKind, serve_asset

if typing.TYPE_CHECKING:
    from collections.abc import Hashable, Iterable

    import dash

_SCRIPT_PATH = Path(__file__).with_name("_layered_picture.js")
_CSS_PATH = Path(__file__).with_name("_layered_picture.css")

_served: WeakSet[dash.Dash] = WeakSet()


class DrawnLayer(BaseModel, frozen=True):
    """A layer to draw over one picture, as the browser is told it: which file it is, and what kind of layer."""

    url: str
    kind: LayerKind
    layer: str
    """The artifact key of the layer, which its style, and a viewer's change, is looked up by."""


def is_overlay_of(key: str, image_key: object) -> bool:
    """Whether `key` is an overlay of the picture under `image_key`."""
    overlay = OverlayKey.parse(key)
    return overlay is not None and overlay.image_key == image_key


def overlay_keys_of(image_key: str, columns: Iterable[str]) -> list[str]:
    """The artifact keys of the overlays logged over the picture under `image_key` that the dataframe `columns` have, in key order."""
    present = set(columns)
    return sorted(
        key
        for key in artifact_keys_in(present, overlay_key(image_key), prefix=True)
        if is_overlay_of(key, image_key)
    )


def drawn_layers(row: dict[Hashable, typing.Any], layers: list[str]) -> list[DrawnLayer]:
    """The layers a picture's `row` of the panel dataframe has a file for, in the order of `layers`."""
    drawn: list[DrawnLayer] = []
    for key in layers:
        url, fname = row.get(artifact_column(key)), row.get(artifact_fname_column(key))
        # A layer the run did not log at this step holds NaN, which is no string.
        if isinstance(url, str) and isinstance(fname, str):
            kind = layer_kind(parse_tags(row.get(artifact_tags_column(key))), fname)
            if kind is not None:
                drawn.append(DrawnLayer(url=url, kind=kind, layer=key))
    return drawn


def serve_layered_picture(app: dash.Dash) -> None:
    """
    Serve the script and stylesheet that draw layers over a picture, once for `app` however many charts ask.

    The script is preceded by the mask defaults the server has, so a mask is drawn in the palette Python uses.
    """
    if app in _served:
        return
    _served.add(app)
    defaults = {
        "DEFAULT_CLASS_COLORS": list(DEFAULT_CLASS_COLORS),
        "DEFAULT_MASK_OPACITY": DEFAULT_MASK_OPACITY,
    }
    serve_asset(
        app,
        AssetKind.SCRIPT,
        _SCRIPT_PATH.name,
        f"window.dlMaskDefaults = {json.dumps(defaults)};\n".encode() + _SCRIPT_PATH.read_bytes(),
    )
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
