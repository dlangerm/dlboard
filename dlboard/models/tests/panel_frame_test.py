"""The panel dataframe's column names, shared by the page's fetch and every chart type."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from dlboard.models import (
    ARTIFACT_COLUMN_PREFIX,
    HISTOGRAM_COLUMN_PREFIX,
    HPARAM_COLUMN_PREFIX,
    ColumnKind,
    SeriesBandRequest,
    artifact_column,
    artifact_fname_column,
    artifact_keys_in,
    artifact_logged_column,
    artifact_tags_column,
    column_kind,
    histogram_column,
    hparam_column,
)

# Keys that share their starts and their slashes, so a prefix asks for some of them and not others. A key spelled like
# a column dlboard makes itself (`artifact__`, `__tags`, ...) is misread: #774.
_KEYS = st.text(alphabet="ab/_", max_size=5).filter(
    lambda key: (
        not key.startswith((HPARAM_COLUMN_PREFIX, ARTIFACT_COLUMN_PREFIX, HISTOGRAM_COLUMN_PREFIX))
        and not key.endswith(("__tags", "__fname", "__logged"))
    )
)


@given(keys=st.lists(_KEYS, unique=True), name=_KEYS, prefix=st.booleans())
@example(keys=["eval/a", "eval/b", "other"], name="eval", prefix=False)  # a prefix is not a key
def test_artifact_keys_in_names_the_keys_asked_for_and_not_their_tag_and_file_name_columns(
    keys: list[str], name: str, *, prefix: bool
) -> None:
    columns = ["run_id", "loss", *(hparam_column(key) for key in keys)]
    for key in keys:
        columns += [artifact_column(key), artifact_tags_column(key), artifact_fname_column(key)]
        columns += [artifact_logged_column(key), histogram_column(key)]

    asked = artifact_keys_in(columns, name, prefix=prefix)

    assert asked == [key for key in keys if (key.startswith(name) if prefix else key == name)]


@given(_KEYS)
@example(key="run_id")
def test_a_column_kind_is_read_back_from_the_name_its_helper_made(key: str) -> None:
    assert column_kind(hparam_column(key)) is ColumnKind.HPARAM
    assert column_kind(artifact_column(key)) is column_kind(artifact_tags_column(key)) is ColumnKind.ARTIFACT
    assert column_kind(histogram_column(key)) is ColumnKind.HISTOGRAM
    assert column_kind(key) is ColumnKind.METRIC


_ACCURACY = [
    "accuracy/1,1/dog",
    "accuracy/1,2/dog",
    "accuracy/1,1/cat",
    "accuracy/1,2/cat",
    "loss",
    "accuracy/1,1/aux/cat",
]


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        pytest.param((), {"": 5}, id="one-band-over-every-match"),
        pytest.param((2,), {"cat": 2, "dog": 2, "aux/cat": 1}, id="a-band-per-class"),
        pytest.param((0, 1), {"1, 1": 3, "1, 2": 2}, id="a-band-per-cell"),
        pytest.param((5,), {"": 5}, id="a-wildcard-that-is-not-there-groups-nothing"),
    ],
)
def test_a_request_groups_the_metrics_its_pattern_matches_by_the_parts_it_names(
    group_by: tuple[int, ...], expected: dict[str, int]
) -> None:
    request = SeriesBandRequest(frozenset({"accuracy/#,#/*"}), grid=8, runs=1, group_by=group_by)

    groups = request.groups(_ACCURACY)

    assert "loss" not in groups
    assert {label: list(groups.values()).count(label) for label in groups.values()} == expected


def test_a_request_reads_only_the_first_groups_by_label() -> None:
    request = SeriesBandRequest(frozenset({"c/#/*"}), grid=8, runs=1, group_by=(1,), max_groups=2)

    groups = request.groups([f"c/{n}/{name}" for n in range(3) for name in "xyz"])

    assert sorted(set(groups.values())) == ["x", "y"]
    assert len(groups) == 6


def test_a_request_column_is_named_by_how_it_groups_too() -> None:
    plain = SeriesBandRequest(frozenset({"a/#/*"}), grid=8, runs=1)

    assert plain.column != SeriesBandRequest(frozenset({"a/#/*"}), grid=8, runs=1, group_by=(1,)).column
    assert plain.column != SeriesBandRequest(frozenset({"a/#/*"}), grid=8, runs=1, max_groups=3).column


@pytest.mark.parametrize(
    ("metrics", "runs", "cells"),
    [(10, 1, 32), (1_000, 3, 32), (11_000, 1, 9), (11_000, 3, 4), (0, 1, 32)],
    ids=["few", "a-thousand-over-three-runs", "eleven-thousand", "the-fewest-cells", "no-metrics"],
)
def test_a_band_over_many_metrics_gets_fewer_cells_to_stay_in_its_probe_budget(
    metrics: int, runs: int, cells: int
) -> None:
    assert SeriesBandRequest(frozenset({"m*"}), grid=32, runs=runs).cells_for(metrics, runs) == cells
