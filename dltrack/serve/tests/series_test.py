from __future__ import annotations

from dltrack.serve import series_color


def test_consecutive_runs_get_distinct_colors_and_a_run_keeps_its_color() -> None:
    sweep = [series_color(run_id) for run_id in range(17, 25)]

    assert len(set(sweep)) == len(sweep)
    assert series_color(17) == series_color(17 + len(sweep))
    assert all(color.startswith("var(--dl-series-") for color in sweep)
