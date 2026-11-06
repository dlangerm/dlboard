"""
A plugin's say over whether a project takes new runs: the guard it registers, asked about every run a request creates.

The core authorizes who may create a run (`AuthorizingDataStore`); a plugin that holds a project to a policy of its own
(retention refusing new runs while the project is far behind its rule) adds what only it knows, and never checks by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash

    from dlboard.models import DataStore

type RunGuard = Callable[[DataStore[...], int], None]
"""Asked, with the system's store (no user is behind it) and the project, before a run is created: raises `ProjectLockedError` to refuse it."""

RUN_GUARDS: AppSlot[list[RunGuard]] = AppSlot("run guards")


def register_run_guard(app: Dash, guard: RunGuard) -> None:
    """Ask `guard` about every run a request creates in `app`, after the writer's role is checked."""
    guards = RUN_GUARDS.find(app)
    if guards is None:
        RUN_GUARDS.set(app, [guard])
    else:
        guards.append(guard)
