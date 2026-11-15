from __future__ import annotations

from pathlib import Path

import dlboard.serve
from dlboard.serve import SeriesDash, series_color, series_dash, series_swatch_class
from dlboard.serve._series import SERIES_SLOTS

_LOOKS = SERIES_SLOTS * len(SeriesDash)
"""How many runs in a row get a color and dash no other of them has."""


def test_consecutive_runs_get_distinct_colors_and_runs_sharing_one_get_distinct_dashes() -> None:
    """#500: runs 8 apart share a color, so they differ in how their lines are dashed, wherever they are drawn."""
    looks = [(series_color(run_id), series_dash(run_id)) for run_id in range(17, 17 + _LOOKS)]

    assert len({color for color, _ in looks[:SERIES_SLOTS]}) == SERIES_SLOTS
    assert len(set(looks)) == _LOOKS
    assert (series_color(17), series_dash(17)) == (series_color(17 + _LOOKS), series_dash(17 + _LOOKS))
    assert all(color.startswith("var(--dl-series-") for color, _ in looks)


def test_the_stylesheet_defines_every_slot_in_both_schemes_and_a_swatch_for_every_dash() -> None:
    css = (Path(dlboard.serve.__file__).parent / "_shell.css").read_text()

    for slot in range(1, SERIES_SLOTS + 1):
        assert css.count(f"--dl-series-{slot}:") == 2, slot  # a light value and a dark one
        assert f".dl-swatch-{slot} {{" in css
    assert f"--dl-series-{SERIES_SLOTS + 1}:" not in css
    for dash in SeriesDash:
        assert (f".dl-swatch-{dash}::before {{" in css) is (dash is not SeriesDash.SOLID), dash
    assert series_swatch_class(_LOOKS) == series_swatch_class(0)
    assert series_swatch_class(SERIES_SLOTS) != series_swatch_class(0)
