"""A chart type is offered by a deployment whose plugin list has it, and only by that one."""

from __future__ import annotations

import subprocess
import sys


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
