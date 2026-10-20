"""The browser draws a mask's default colors from its own copy of the palette; it must equal the client's."""

import re
from pathlib import Path

import dlboard.plugins.charts
from dlboard.client.artifacts.overlay import DEFAULT_CLASS_COLORS, DEFAULT_MASK_OPACITY

_OVERLAY_JS = Path(dlboard.plugins.charts.__file__).with_name("image_series_overlay.js").read_text()


def test_the_overlay_script_draws_masks_in_the_clients_default_palette_and_opacity() -> None:
    palette = re.search(r"DEFAULT_CLASS_COLORS = \[(.*?)\]", _OVERLAY_JS, re.DOTALL)
    opacity = re.search(r"DEFAULT_MASK_OPACITY = ([\d.]+);", _OVERLAY_JS)

    assert palette is not None
    assert tuple(re.findall(r'"(#[0-9a-fA-F]{6})"', palette.group(1))) == DEFAULT_CLASS_COLORS
    assert opacity is not None
    assert float(opacity.group(1)) == DEFAULT_MASK_OPACITY
