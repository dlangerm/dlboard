"""How many runs a comparison may hold."""

from __future__ import annotations

from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class ComparisonSettings(BaseSettings):
    """`DLBOARD_*` environment variables bounding a comparison."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    max_comparison_runs: PositiveInt = 100
    """
    The most runs a comparison holds, however many it is given: the store refuses one that would hold more.

    What bounds what a comparison page reads, since each run's metrics are read for every chart on it. Kept apart from
    `DLBOARD_MAX_SHOWN_RUNS`, the most a view selects to draw in an experiment's run table: a comparison is picked by hand
    and kept, and may reasonably hold more than an ad-hoc selection does.
    """
