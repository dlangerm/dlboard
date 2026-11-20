"""A chart type is offered by a deployment whose plugin list has it, and only by that one."""

from __future__ import annotations

import subprocess
import sys

from dash import Dash

from dlboard.models import ChartTypeRegistry
from dlboard.plugins import BUILTIN_CHARTS


def test_importing_the_plugins_registers_no_chart_type() -> None:
    """
    Run in a subprocess: this suite's own imports and apps have registered every built-in chart long before a test runs.

    `dlboard.plugins` imports every built-in chart module, so one that registered its type at import would offer it in a
    deployment that left it out of its plugin list, without the scripts and stylesheet its `plug()` serves.
    """
    script = """
from dlboard import plugins
from dlboard.models import ChartTypeRegistry
print(sorted(ChartTypeRegistry.get_registered_chart_types()))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_every_built_in_chart_type_is_a_whole_chart_type_the_editor_can_describe() -> None:
    """A chart type is never instantiated, so a classmethod it leaves abstract goes unnoticed until the editor calls it."""
    app = Dash(__name__)
    for plugin in BUILTIN_CHARTS:
        plugin.plug(app)
    described = ChartTypeRegistry.get_registered_chart_types()

    unfinished = {
        name: sorted(chart.__abstractmethods__)
        for name in described
        if (chart := ChartTypeRegistry.get_chart_type(name)).__abstractmethods__
    }
    assert not unfinished
