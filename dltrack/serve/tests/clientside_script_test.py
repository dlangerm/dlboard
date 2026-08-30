from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dltrack.serve import ClientsideScript

if TYPE_CHECKING:
    from pathlib import Path


def test_clientside_script_asserts_the_path_exists(tmp_path: Path) -> None:
    with pytest.raises(AssertionError):
        ClientsideScript(tmp_path / "missing.js")


def test_clientside_script_reads_the_file_contents(tmp_path: Path) -> None:
    path = tmp_path / "script.js"
    path.write_text("function foo(n) { return n; }")

    assert ClientsideScript(path).source == "function foo(n) { return n; }"
