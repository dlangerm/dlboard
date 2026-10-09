"""Tests for the file-list chart: one row per run and step, each with a download link."""

from __future__ import annotations

import json
from typing import Any, cast

import pandas as pd
import pytest
from dash._utils import to_json  # pyright: ignore[reportUnknownVariableType]

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


def _render(df: pd.DataFrame, key: str = "ckpt") -> list[dict[str, Any]]:
    return _nodes(json.loads(cast("str", to_json(FileListChart.render(FileListSettings(key=key), df)))))


def _files_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "run_id": [2, 1, 1],
            "step": [0, 1, 0],
            artifact_column("ckpt"): ["/artifact/3?v=c", "/artifact/2?v=b", "/artifact/1?v=a"],
            artifact_tags_column("ckpt"): [{"tag": "best"}, {"tag": "latest", "score": "0.5"}, {}],
            RUN_NAME_COLUMN: ["beta", "alpha", "alpha"],
        }
    )


def test_render_links_every_file_in_run_then_step_order() -> None:
    nodes = _render(_files_df())

    hrefs = [n["props"]["href"] for n in nodes if n["type"] == "Anchor"]
    assert hrefs == ["/artifact/1?v=a", "/artifact/2?v=b", "/artifact/3?v=c"]


def test_render_labels_each_row_with_its_run_name_step_and_tags() -> None:
    cells = [n["props"]["children"] for n in _render(_files_df()) if n["type"] == "Text"]

    assert cells == ["alpha", "0", "", "alpha", "1", "tag: latest, score: 0.5", "beta", "0", "tag: best"]


@pytest.mark.parametrize(
    "df",
    [
        pytest.param(pd.DataFrame({"run_id": [1], "step": [0]}), id="no-such-key"),
        pytest.param(
            pd.DataFrame({"run_id": [1], "step": [0], artifact_column("ckpt"): [float("nan")]}),
            id="only-gaps",
        ),
    ],
)
def test_render_says_so_when_there_is_nothing_logged_under_the_key(df: pd.DataFrame) -> None:
    texts = [n["props"]["children"] for n in _render(df) if n["type"] == "Text"]

    assert texts == ["No artifacts logged for key 'ckpt'"]


def test_the_size_of_the_list_is_a_setting() -> None:
    settings = FileListSettings(key="ckpt", height=120, width=640)

    assert FileListChart.natural_width(settings) == 640
    rendered = _nodes(json.loads(cast("str", to_json(FileListChart.render(settings, _files_df())))))
    scroll_area = next(n for n in rendered if n["type"] == "ScrollArea")
    assert scroll_area["props"]["mah"] == 120
