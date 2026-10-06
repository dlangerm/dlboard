from __future__ import annotations

from pathlib import Path

import dlboard.serve
from dlboard.serve import series_color, series_swatch_class
from dlboard.serve._series import SERIES_SLOTS


def test_consecutive_runs_get_distinct_colors_and_a_run_keeps_its_color() -> None:
    sweep = [series_color(run_id) for run_id in range(17, 25)]

    assert len(set(sweep)) == len(sweep)
    assert series_color(17) == series_color(17 + len(sweep))
    assert all(color.startswith("var(--dl-series-") for color in sweep)


def test_the_stylesheet_defines_every_slot_in_both_schemes_with_a_swatch() -> None:
    css = (Path(dlboard.serve.__file__).parent / "_shell.css").read_text()

    for slot in range(1, SERIES_SLOTS + 1):
        assert css.count(f"--dl-series-{slot}:") == 2, slot  # a light value and a dark one
        assert f".dl-swatch-{slot} {{" in css
    assert f"--dl-series-{SERIES_SLOTS + 1}:" not in css
    assert series_swatch_class(SERIES_SLOTS) == series_swatch_class(0)
