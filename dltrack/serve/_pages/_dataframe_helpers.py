"""Pure dataframe-building helpers shared by more than one page. No Dash, no Page classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dltrack.models import Experiment


def experiment_display_name(experiment: Experiment) -> str:
    """Human-readable name for an experiment, falling back to its id when unnamed."""
    return experiment.name or f"Experiment {experiment.id}"
