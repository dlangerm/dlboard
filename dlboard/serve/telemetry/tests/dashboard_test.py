"""
The metrics dashboard and `docs/telemetry.md` say what the code defines: an instrument nobody charted or documented fails here.

The dashboard's queries are held to the names `/metrics` really serves, found by recording once to each instrument and
seeing which series appear, not to a naming rule written out a second time here.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path
from typing import Any

import pytest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel

import dlboard.plugins
from dlboard.serve.telemetry import INSTRUMENTS, Counter, Histogram, _Instrument
from dlboard.serve.telemetry._prometheus import serve_metrics

_DOCS = Path(__file__).parents[4] / "docs"
_DASHBOARD = _DOCS / "grafana" / "dlboard.json"
_CATALOGUE = _DOCS / "telemetry.md"

NOT_CHARTED: dict[str, str] = {}
"""An instrument deliberately left off the dashboard, with why. Empty: everything is charted."""


class _Target(BaseModel):
    expr: str


class _Panel(BaseModel):
    targets: list[_Target] = []


class _Dashboard(BaseModel):
    """The part of an exported Grafana dashboard this test reads."""

    panels: list[_Panel]


def _defined_instruments() -> dict[str, _Instrument[Any]]:
    """
    Every instrument of dlboard itself, tests' own excluded.

    The server (`dlboard.serve.app`) and every plugin are imported first. Not every module under `dlboard.serve`: its pages
    register themselves with Dash on import, and `migrations/env.py` only runs under Alembic.
    """
    for server_module in ("dlboard.serve.app", "dlboard.serve._production_server"):
        importlib.import_module(server_module)
    for module in pkgutil.walk_packages(dlboard.plugins.__path__, f"{dlboard.plugins.__name__}."):
        if not {"tests", "_tests"} & set(module.name.split(".")):
            importlib.import_module(module.name)
    return {name: i for name, i in INSTRUMENTS.items() if not name.startswith("test.")}


def _series() -> set[str]:
    return {
        sample.name
        for family in text_string_to_metric_families(serve_metrics().get_data(as_text=True))
        for sample in family.samples
    }


@pytest.fixture
def served(telemetry: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, set[str]]:
    """Each instrument's `/metrics` series, found by recording to them one at a time."""
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_DIR", str(tmp_path))
    found: dict[str, set[str]] = {}
    seen = _series()
    for name, instrument in _defined_instruments().items():
        match instrument:
            case Histogram():
                instrument.record(1)
            case Counter():
                instrument.add(1)
        found[name] = (now := _series()) - seen
        seen = now
    return found


def _queried() -> set[str]:
    """Every series the dashboard's queries read, leaving out the label names a `by (...)` or `{...}` names."""
    dashboard = _Dashboard.model_validate_json(_DASHBOARD.read_text())
    exprs = (target.expr for panel in dashboard.panels for target in panel.targets)
    return {
        token
        for expr in exprs
        for token in re.findall(r"[a-z_]+", re.sub(r"by \([^)]*\)|\{[^}]*\}", "", expr))
    }


def test_every_instrument_is_in_the_catalogue() -> None:
    documented = set(re.findall(r"^\| `([a-z._]+)`", _CATALOGUE.read_text(), re.MULTILINE))

    assert set(_defined_instruments()) - documented == set()
    assert documented - set(_defined_instruments()) == set()


def test_every_instrument_is_charted_or_waived(served: dict[str, set[str]]) -> None:
    queried = _queried()

    uncharted = {name for name, series in served.items() if not series & queried and name not in NOT_CHARTED}

    assert uncharted == set()


def test_every_query_reads_a_series_that_is_served(served: dict[str, set[str]]) -> None:
    every_series = set().union(*served.values())

    unserved = {token for token in _queried() if re.match(r"(dlboard|http)_", token)} - every_series

    assert unserved == set()
