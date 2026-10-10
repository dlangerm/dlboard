"""Tests for the dialogs that auto-generate and suggest charts."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from dash._utils import to_json

from dlboard.models._view import ColumnKind
from dlboard.serve._pages._experiment import _chart_suggestions as suggestions


def _nodes(node: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    """Every component in a serialized Dash tree, depth first."""
    match node:
        case {"props": props}:
            return [
                cast("dict[str, Any]", node),
                *_nodes(props.get("children")),
            ]  # pyrefly: ignore [unknown-argument-type]
        case list():
            return [found for child in cast("list[Any]", node) for found in _nodes(child)]
        case _:
            return []


def _preview(n_panels: int) -> list[dict[str, Any]]:
    groups = {f"panel {i}": [(f"key{i}", ColumnKind.METRIC)] for i in range(n_panels)}
    return _nodes(json.loads(cast("str", to_json(suggestions._render_auto_preview(groups)))))


@pytest.mark.parametrize(
    ("n_panels", "listed", "more"),
    [(3, 3, None), (20, 20, None), (21, 20, "…and 1 more panel"), (239, 20, "…and 219 more panels")],
)
def test_the_preview_lists_a_bounded_number_of_panels_but_counts_them_all(
    n_panels: int, listed: int, more: str | None
) -> None:
    """Rendering 239 of them took the browser about three seconds, for a list nobody can read."""
    nodes = _preview(n_panels)

    texts = [
        n["props"]["children"]
        for n in nodes
        if n["type"] == "Text" and isinstance(n["props"]["children"], str)
    ]
    assert sum(n["type"] == "Paper" for n in nodes) == listed
    assert texts[0] == f"{n_panels} panel{'s' if n_panels != 1 else ''} · {n_panels} charts"
    assert [t for t in texts if t.startswith("…and")] == ([more] if more else [])
