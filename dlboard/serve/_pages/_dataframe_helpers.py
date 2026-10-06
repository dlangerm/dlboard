"""Pure dataframe-building helpers shared by more than one page. No Dash, no Page classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dlboard.models import Experiment, Run


def experiment_display_name(experiment: Experiment) -> str:
    """Human-readable name for an experiment, falling back to its id when unnamed."""
    return experiment.name or f"Experiment {experiment.id}"


def run_display_name(run: Run) -> str:
    """Human-readable name for a run, falling back to its id when unnamed."""
    return run.name or f"Run {run.id}"
