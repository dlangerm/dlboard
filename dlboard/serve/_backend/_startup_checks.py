"""Checks a plugin registers from `plug()` that only make sense once the whole app is built."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash

_STARTUP_CHECKS: AppSlot[list[Callable[[Dash], None]]] = AppSlot("startup checks")


def add_startup_check(app: Dash, check: Callable[[Dash], None]) -> None:
    """
    Run `check(app)` once every plugin has plugged in, failing startup if it raises.

    For a check that reads state another plugin sets (the data store, say): plugin order isn't
    guaranteed, so `plug()` itself can't rely on it being there yet.
    """
    checks: list[Callable[[Dash], None]] | None = _STARTUP_CHECKS.find(app)
    if checks is None:
        checks = []
        _STARTUP_CHECKS.set(app, checks)
    checks.append(check)


def run_startup_checks(app: Dash) -> None:
    """Run every check `add_startup_check` registered on `app`, in registration order."""
    for check in _STARTUP_CHECKS.find(app) or []:
        check(app)
