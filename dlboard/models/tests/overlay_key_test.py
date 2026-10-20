"""An overlay's key is its image's key plus a suffix and an optional name, and reads back to both."""

from __future__ import annotations

import pytest

from dlboard.models import (
    OVERLAY_KEY_SUFFIX,
    UNNAMED_OVERLAY,
    OverlayKey,
    artifact_display_name,
    overlay_key,
)


@pytest.mark.parametrize(
    ("image_key", "name", "key", "label", "display"),
    [
        pytest.param(
            "samples/a",
            None,
            f"samples/a{OVERLAY_KEY_SUFFIX}",
            UNNAMED_OVERLAY,
            f"samples/a · {UNNAMED_OVERLAY}",
            id="unnamed",
        ),
        pytest.param(
            "samples/a",
            "truth",
            f"samples/a{OVERLAY_KEY_SUFFIX}/truth",
            "truth",
            "samples/a · truth",
            id="named",
        ),
    ],
)
def test_an_overlay_key_reads_back_to_its_image_key_and_name(
    image_key: str, name: str | None, key: str, label: str, display: str
) -> None:
    assert overlay_key(image_key, name) == key
    assert OverlayKey.parse(key) == OverlayKey(image_key, name)
    assert OverlayKey(image_key, name).label == label
    assert artifact_display_name(key) == display


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("samples/a", id="no-suffix"),
        pytest.param(f"samples/a{OVERLAY_KEY_SUFFIX}x", id="suffix-is-part-of-a-longer-word"),
        pytest.param(OVERLAY_KEY_SUFFIX, id="no-image"),
    ],
)
def test_a_key_that_is_not_an_overlays_is_shown_as_it_is(key: str) -> None:
    assert OverlayKey.parse(key) is None
    assert artifact_display_name(key) == key
