"""Table to store a json blob of hyperparameters for an experiment."""

from __future__ import annotations

import json
from typing import Any, TypeAlias

from pydantic import BaseModel
from structlog.stdlib import get_logger

_log = get_logger(__name__)

ValidJsonTypes: TypeAlias = str | int | float | bool | None
FlatHparamDict: TypeAlias = dict[str, ValidJsonTypes]
RawHparamDict: TypeAlias = dict[Any, Any]


class NewHyperParams(BaseModel, frozen=True, extra="forbid"):
    """A new hyperparameter blob to store in the database."""

    run_id: int
    """The run ID for this set of hyperparameters."""

    experiment_id: int
    """Experiment attached to the run."""

    raw_hparams: str
    """The hyperparameters to store."""

    @classmethod
    def from_raw(cls, run_id: int, experiment_id: int, hparams: RawHparamDict) -> NewHyperParams:
        """Create a new hyperparameter blob from a raw dictionary."""
        return cls(run_id=run_id, experiment_id=experiment_id, raw_hparams=json.dumps(hparams))

    @property
    def hparams_dict(self) -> FlatHparamDict:
        """Get the hyperparameters as a dictionary."""
        return json.loads(self.raw_hparams)


class HyperParams(NewHyperParams, frozen=True, extra="forbid"):
    """A hyperparameter blob stored in the database."""

    id: int
    """The ID of this hyperparameter blob."""
