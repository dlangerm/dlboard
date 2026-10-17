"""An overlay's key is its image's key plus a suffix and an optional name, and reads back to the image's."""

from __future__ import annotations

import pytest

from dlboard.models import OVERLAY_KEY_SUFFIX, overlay_image_key, overlay_key


@pytest.mark.parametrize(
    ("image_key", "name", "key"),
    [
        pytest.param("samples/a", None, f"samples/a{OVERLAY_KEY_SUFFIX}", id="unnamed"),
        pytest.param("samples/a", "truth", f"samples/a{OVERLAY_KEY_SUFFIX}/truth", id="named"),
    ],
)
def test_an_overlay_key_reads_back_to_its_image_key(image_key: str, name: str | None, key: str) -> None:
    assert overlay_key(image_key, name) == key
    assert overlay_image_key(key) == image_key


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("samples/a", id="no-suffix"),
        pytest.param(f"samples/a{OVERLAY_KEY_SUFFIX}x", id="suffix-is-part-of-a-longer-word"),
        pytest.param(OVERLAY_KEY_SUFFIX, id="no-image"),
    ],
)
def test_a_key_that_is_not_an_overlays_has_no_image_key(key: str) -> None:
    assert overlay_image_key(key) is None
