"""Tests for `dlboard.models.InstalledPlugin.describe`."""

from __future__ import annotations

import inspect

from dlboard import models
from dlboard.plugins.auth import anonymous


class _UndocumentedPlugin:
    @classmethod
    def plug(cls, app: object) -> None:
        pass


def test_describe_uses_the_plugin_modules_dotted_name_and_first_docstring_line() -> None:
    described = models.InstalledPlugin.describe(anonymous)

    assert described.name == "dlboard.plugins.auth.anonymous"
    doc = inspect.getdoc(anonymous)
    assert doc is not None
    assert described.description == doc.splitlines()[0]


def test_describe_has_no_description_for_an_undocumented_plugin() -> None:
    described = models.InstalledPlugin.describe(_UndocumentedPlugin)

    assert described.name == _UndocumentedPlugin.__module__
    assert described.description is None
