"""An artifact's tags, as a chart's caption shows them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard.models import Quantity, SizeUnit, TagDisplay, TimeUnit
from dlboard.plugins.charts._artifact_tags import format_tags, format_tags_caption

if TYPE_CHECKING:
    from dlboard import models


@pytest.mark.parametrize(
    ("tags", "caption"),
    [
        ({"split": "val"}, "split: val"),
        ({"score": 0.140984, "epoch": 3, "best": True}, "score: 0.1410, epoch: 3, best: true"),
        ({"score": "0.1410"}, "score: 0.1410"),
        (
            {"a": 0.14377, "b": 5.0, "c": 1e-05, "d": False, "e": ""},
            "a: 0.1438, b: 5.000, c: 1.000e-05, d: false, e: ",
        ),
        (
            {
                "mem": Quantity(value=2.5e9, unit=SizeUnit.BYTES),
                "took": Quantity(value=5400, unit=TimeUnit.SECONDS),
            },
            "mem: 2.500 GB, took: 1.500 h",
        ),
        (
            {
                "short": Quantity(value=0.0003, unit=TimeUnit.SECONDS),
                "none": Quantity(value=0, unit=SizeUnit.GIGABYTES),
            },
            "short: 300.0 us, none: 0.000 GB",
        ),
        (
            {
                "fast": Quantity(value=0.0000025, unit=TimeUnit.SECONDS),
                "tiny": Quantity(value=0.4, unit=TimeUnit.MICROSECONDS),
            },
            "fast: 2.500 us, tiny: 400.0 ns",
        ),
        ({"big": 12345.678, "huge": 3e20}, "big: 12,346, huge: 3.000e+20"),
        (float("nan"), ""),
    ],
    ids=[
        "text",
        "scalars",
        "text-matches-scalar",
        "float-formats",
        "quantities",
        "quantity-edges",
        "sub-millisecond",
        "large",
        "gap",
    ],
)
def test_format_tags_caption_formats_scalars_consistently(
    tags: dict[str, models.TagValue] | float, caption: str
) -> None:
    assert format_tags_caption(tags) == caption


@pytest.mark.parametrize(
    ("display", "caption"),
    [
        ({}, "took: 1.500 h, mem: 2.500 GB"),
        ({"time_unit": TimeUnit.MINUTES, "size_unit": SizeUnit.MEGABYTES}, "took: 90.00 min, mem: 2,500 MB"),
        ({"time_unit": TimeUnit.MILLISECONDS}, "took: 5,400,000 ms, mem: 2.500 GB"),
        ({"precision": 2}, "took: 1.5 h, mem: 2.5 GB"),
        ({"precision": 6, "size_unit": SizeUnit.KILOBYTES}, "took: 1.50000 h, mem: 2,500,000 kB"),
    ],
    ids=["auto", "units", "finer-unit", "precision", "precision-and-unit"],
)
def test_the_display_picks_the_unit_and_precision_not_the_client(
    display: dict[str, int | TimeUnit | SizeUnit], caption: str
) -> None:
    """The client says what a number was measured in; how it is shown is the display's call."""
    tags = {
        "took": Quantity(value=5400, unit=TimeUnit.SECONDS),
        "mem": Quantity(value=2.5, unit=SizeUnit.GIGABYTES),
    }
    assert format_tags_caption(tags, TagDisplay(**display)) == caption


def test_a_tag_named_in_the_per_key_map_overrides_the_charts_default_display() -> None:
    tags: dict[str, models.TagValue] = {
        "took": Quantity(value=5400, unit=TimeUnit.SECONDS),
        "wait": Quantity(value=5400, unit=TimeUnit.SECONDS),
        "score": 0.140984,
    }
    default = TagDisplay(precision=2, time_unit=TimeUnit.MINUTES)
    by_key = {"took": TagDisplay(time_unit=TimeUnit.HOURS), "score": TagDisplay(precision=6)}

    assert format_tags_caption(tags, default, by_key) == "took: 1.5 h, wait: 90 min, score: 0.140984"
    assert format_tags(tags, default, by_key) == {"took": "1.5 h", "wait": "90 min", "score": "0.140984"}
