"""
What the plugin skill (`skills/dlboard-plugin/SKILL.md`) names exists, and what it calls public API is exported.

The skill is copied into other projects and followed by an agent that trusts it, so a file it says to read or a function
it says to call that has since moved has the agent write a plugin against something that is not there.
"""

import importlib
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_SKILL = (_ROOT / "skills" / "dlboard-plugin" / "SKILL.md").read_text()
_PUBLIC = ("dlboard.serve", "dlboard.models", "dlboard.plugins.data_stores", "dlboard.plugins")
"""Where a plugin's API comes from: the modules whose `__all__` the skill tells the agent to keep to, and the bundles."""


def _exported(name: str) -> bool:
    return any(name in importlib.import_module(module).__all__ for module in _PUBLIC)


@pytest.mark.parametrize(
    "path",
    sorted(
        set(re.findall(r"`(dlboard/[\w/.]+)`", _SKILL))
        | {f"docs/{path}" for path in re.findall(r"`([\w/-]+\.md)`", _SKILL)}
    ),
)
def test_every_file_it_points_to_exists(path: str) -> None:
    assert (_ROOT / path).exists()


@pytest.mark.parametrize(
    ("module", "name"),
    sorted(
        {
            (module, name.split(" as ")[0])
            for module, names in re.findall(r"^from (dlboard[\w.]*) import (.+)$", _SKILL, re.MULTILINE)
            for name in names.split(", ")
        }
    ),
)
def test_every_name_it_imports_exists(module: str, name: str) -> None:
    assert hasattr(importlib.import_module(module), name)


@pytest.mark.parametrize(
    "name",
    sorted(
        set(
            re.findall(
                r"`((?:set|register|add|get|serve|plug)_\w+|[A-Z]\w*[a-z]\w*|[A-Z][A-Z0-9_]*[A-Z0-9])`",
                _SKILL,
            )
        )
    ),
)
def test_every_function_class_and_bundle_it_names_is_public(name: str) -> None:
    assert _exported(name), f"`{name}` is not exported by any of {', '.join(_PUBLIC)}"
