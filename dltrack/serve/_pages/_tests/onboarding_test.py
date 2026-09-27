from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

import pytest
from flask import Flask

from dltrack.serve._pages._onboarding import (
    first_experiment_snippet,
    first_project_snippet,
    first_run_snippet,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SERVER = "http://tracker:8050/dl"


def _logger_call(snippet: str) -> tuple[str, tuple[Any, ...], dict[str, Any]]:
    """What `logger = ...` in `snippet` calls, with which positional and keyword arguments."""
    (call,) = [
        node.value
        for node in ast.parse(snippet).body
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "logger"
    ]
    assert isinstance(call, ast.Call)
    return (
        ast.unparse(call.func),
        tuple(ast.literal_eval(arg) for arg in call.args),
        {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg},
    )


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (first_project_snippet, ("DLTrackLogger.from_names", ("my-project",), {"server_url": _SERVER})),
        (
            lambda: first_experiment_snippet(4),
            ("DLTrackLogger", (), {"project_id": 4, "server_url": _SERVER}),
        ),
        (
            lambda: first_run_snippet(4, 9),
            ("DLTrackLogger", (), {"project_id": 4, "experiment_id": 9, "server_url": _SERVER}),
        ),
    ],
)
def test_a_snippet_logs_to_the_server_the_browser_used(
    build: Callable[[], str], expected: tuple[str, tuple[Any, ...], dict[str, Any]]
) -> None:
    # Behind a reverse proxy at `/dl/`: the prefix is part of the server URL, the trailing slash isn't.
    with Flask(__name__).test_request_context(base_url=f"{_SERVER}/"):
        snippet = build()

    assert _logger_call(snippet) == expected
