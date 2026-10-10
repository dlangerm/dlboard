"""Tests for the file-list chart: files by run and step with download links, grouped by kind and paged."""

from __future__ import annotations

import json
from typing import Any, cast

import pandas as pd
import pytest
from dash._utils import to_json  # pyright: ignore[reportUnknownVariableType]
from pydantic import ValidationError

from dlboard.models import RUN_NAME_COLUMN
from dlboard.plugins.charts._table_style import artifact_column, artifact_tags_column
from dlboard.plugins.charts.file_list import FileListChart, FileListSettings


def _nodes(node: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Every component in a serialized Dash tree, depth first."""
    match node:
        case {"props": props}:
            return [cast("dict[str, Any]", node), *_nodes(props.get("children"))]
        case list():
            return [found for child in cast("list[Any]", node) for found in _nodes(child)]
        case _:
            return []


def _render(df: pd.DataFrame, settings: FileListSettings) -> list[dict[str, Any]]:
    return _nodes(json.loads(cast("str", to_json(FileListChart.render(settings, df)))))


def _grids(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [n["props"] for n in nodes if n["type"] == "AgGrid"]


def _checkpoints_df() -> pd.DataFrame:
    """Two runs' checkpoints under `ckpt/`, one key per file as `DLBoardLogger` logs them."""
    return pd.DataFrame(
        {
            "run_id": [2, 1, 1, 1],
            "step": [0, 1, 0, 2],
            artifact_column("ckpt/a.ckpt"): [
                "/artifact/3?v=c",
                "/artifact/2?v=b",
                float("nan"),
                float("nan"),
            ],
            artifact_tags_column("ckpt/a.ckpt"): [
                {"tag": "best", "score": "0.9"},
                {"tag": "latest"},
                float("nan"),
                float("nan"),
            ],
            artifact_column("ckpt/b.ckpt"): [
                float("nan"),
                float("nan"),
                "/artifact/1?v=a",
                "/artifact/4?v=d",
            ],
            artifact_tags_column("ckpt/b.ckpt"): [
                float("nan"),
                float("nan"),
                {"tag": "best_k"},
                {"tag": "best_k"},
            ],
            artifact_column("other"): ["/artifact/9?v=z", float("nan"), float("nan"), float("nan")],
            RUN_NAME_COLUMN: ["beta", "alpha", "alpha", "alpha"],
        }
    )


def test_a_prefix_lists_every_file_under_it_by_run_then_step() -> None:
    nodes = _render(_checkpoints_df(), FileListSettings(key_prefix="ckpt/"))

    rows = [row for grid in _grids(nodes) for row in grid["rowData"]]

    assert sorted((row["run"], row["step"], row["file"]) for row in rows) == [
        ("alpha", 0, "[b.ckpt](/artifact/1?v=a)"),
        ("alpha", 1, "[a.ckpt](/artifact/2?v=b)"),
        ("alpha", 2, "[b.ckpt](/artifact/4?v=d)"),
        ("beta", 0, "[a.ckpt](/artifact/3?v=c)"),
    ]


def test_one_key_lists_only_that_key() -> None:
    nodes = _render(_checkpoints_df(), FileListSettings(key="other"))

    [grid] = _grids(nodes)

    assert [(row["run"], row["file"]) for row in grid["rowData"]] == [("beta", "[other](/artifact/9?v=z)")]


def test_files_are_split_into_a_tab_per_kind_with_the_ones_you_look_for_first() -> None:
    nodes = _render(_checkpoints_df(), FileListSettings(key_prefix="ckpt/"))

    assert [n["props"]["value"] for n in nodes if n["type"] == "TabsTab"] == ["Latest", "Best", "Top-k"]
    assert [n["props"]["children"] for n in nodes if n["type"] == "Badge"] == ["1", "1", "2"]
    assert [grid["rowData"][0]["group"] for grid in _grids(nodes)] == ["Latest", "Best", "Top-k"]


def test_a_score_column_only_appears_where_something_has_a_score() -> None:
    nodes = _render(_checkpoints_df(), FileListSettings(key_prefix="ckpt/"))

    fields = {
        grid["rowData"][0]["group"]: [column["field"] for column in grid["columnDefs"]]
        for grid in _grids(nodes)
    }

    assert fields == {
        "Latest": ["run", "file", "step"],
        "Best": ["run", "file", "step", "score"],
        "Top-k": ["run", "file", "step"],
    }


def test_files_of_one_kind_get_no_tabs() -> None:
    df = _checkpoints_df()
    df[artifact_tags_column("other")] = pd.Series(
        [{"file_kind": "checkpoint"}, None, None, None], dtype=object
    )

    nodes = _render(df, FileListSettings(key="other"))

    assert not [n for n in nodes if n["type"] == "TabsTab"]
    assert len(_grids(nodes)) == 1


def test_the_page_size_is_a_setting() -> None:
    [grid] = _grids(_render(_checkpoints_df(), FileListSettings(key="other", page_size=25)))

    assert grid["dashGridOptions"]["pagination"] is True
    assert grid["dashGridOptions"]["paginationPageSize"] == 25


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(FileListSettings(key="nope"), id="no-such-key"),
        pytest.param(FileListSettings(key_prefix="nope/"), id="no-such-prefix"),
    ],
)
def test_render_says_so_when_there_is_nothing_logged(settings: FileListSettings) -> None:
    texts = [n["props"]["children"] for n in _render(_checkpoints_df(), settings) if n["type"] == "Text"]

    assert texts == [f"No artifacts logged for '{settings.label}'"]


@pytest.mark.parametrize("fields", [{}, {"key": "a", "key_prefix": "b/"}])
def test_a_list_is_of_exactly_one_key_or_prefix(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        FileListSettings.model_validate(fields)


def test_what_the_list_needs_fetched_follows_whether_it_names_a_key_or_a_prefix() -> None:
    by_key, by_prefix = FileListSettings(key="a"), FileListSettings(key_prefix="b/")

    assert FileListChart.hint_required_artifact_keys(by_key) == {"a"}
    assert FileListChart.hint_required_artifact_key_prefixes(by_key) == set()
    assert FileListChart.hint_required_artifact_keys(by_prefix) == set()
    assert FileListChart.hint_required_artifact_key_prefixes(by_prefix) == {"b/"}
