"""Table to store a json blob of hyperparameters for an experiment."""

from __future__ import annotations

import json
from typing import TypeAlias

from pydantic import BaseModel, TypeAdapter
from structlog.stdlib import get_logger

_log = get_logger(__name__)

ValidJsonTypes: TypeAlias = str | int | float | bool | None
FlatHparamDict: TypeAlias = dict[str, ValidJsonTypes]
_flat_hparam_dict_adapter: TypeAdapter[FlatHparamDict] = TypeAdapter(FlatHparamDict)


class NewHyperParams(BaseModel, frozen=True, extra="ignore"):
    """A new hyperparameter blob to store in the database. `extra="ignore"`: this crosses the wire -- see `dltrack._wire`."""

    run_id: int
    """The run ID for this set of hyperparameters."""

    experiment_id: int
    """Experiment attached to the run."""

    raw_hparams: str
    """The hyperparameters to store."""

    @classmethod
    def from_raw(cls, run_id: int, experiment_id: int, hparams: FlatHparamDict) -> NewHyperParams:
        """
        Create a new hyperparameter blob from a flat dict of JSON-scalar values.

        Validated against `FlatHparamDict` up front -- the same shape `hparams_dict` promises back
        on read -- so a caller handing over something that doesn't actually fit (a nested dict, a
        list, some other object) fails loudly right here instead of silently round-tripping into
        a table that later can't make sense of it. Values otherwise pass through untouched: a
        value logged as the string `"128"` stays the string `"128"`, not the number `128` --
        dltrack doesn't know or guess what a hyperparameter's caller intended, so it never
        reinterprets one after the fact.
        """
        validated = _flat_hparam_dict_adapter.validate_python(hparams)
        return cls(run_id=run_id, experiment_id=experiment_id, raw_hparams=json.dumps(validated))

    @property
    def hparams_dict(self) -> FlatHparamDict:
        """Get the hyperparameters as a dictionary."""
        return json.loads(self.raw_hparams)


class HyperParams(NewHyperParams, frozen=True, extra="ignore"):
    """A hyperparameter blob stored in the database. `extra="ignore"`: this crosses the wire -- see `dltrack._wire`."""

    id: int
    """The ID of this hyperparameter blob."""
