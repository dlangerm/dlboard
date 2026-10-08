"""
Every environment variable a `BaseSettings` class reads is documented in `docs/configuration.md`.

Spans every settings class in the package (server, plugins and client) and the docs file, so a new
setting added without documenting it fails here instead of shipping as a hidden knob.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

import dlboard

_DOC = Path(__file__).parents[2] / "docs" / "configuration.md"


def _env_vars() -> list[str]:
    """The env var of every field of every non-test `BaseSettings` class defined in `dlboard`."""
    for module in pkgutil.walk_packages(dlboard.__path__, "dlboard."):
        # These do real work on import (Alembic's `env.py`, the dev/deployment scripts, Granian's
        # WSGI target, Dash's flat page files that register with a running app); none define settings.
        if not re.search(
            r"\.(tests|migrations|scripts)\.|\._wsgi$|^dlboard\.serve\._pages\.[a-z]+$", module.name
        ):
            importlib.import_module(module.name)
    found: set[str] = set()
    pending = BaseSettings.__subclasses__()
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("dlboard.") and ".tests." not in cls.__module__:
            prefix = cls.model_config.get("env_prefix", "")
            found.update(f"{prefix}{name}".upper() for name in cls.model_fields)
    return sorted(found)


_DOCUMENTED = set(re.findall(r"`(DLBOARD_[A-Z0-9_]+)`", _DOC.read_text()))


@pytest.mark.parametrize("env_var", _env_vars())
def test_every_setting_is_documented(env_var: str) -> None:
    assert env_var in _DOCUMENTED, f"add `{env_var}` to docs/configuration.md"


def test_the_documented_settings_all_exist() -> None:
    assert set(_env_vars()) >= _DOCUMENTED, "docs/configuration.md lists a setting nothing reads"
