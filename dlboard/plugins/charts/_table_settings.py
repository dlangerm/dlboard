"""A table chart's settings, and how a table saved with the settings it had before rows and values becomes them."""

from __future__ import annotations

import typing

from pydantic import BaseModel, Field, model_validator

from dlboard._compat import StrEnum
from dlboard.models import DISPLAY_SECTION, NamePatternList


class TableRows(StrEnum):
    """What a table's rows are. Saved with a chart, so a value is never renamed (`persisted_enum_values_test`)."""

    RUN = "run"
    """One row per run."""
    FIELD = "field"
    """One row per distinct value of `row_field` (every step, say) -- of each run, or all runs side by side."""
    ARTIFACT = "artifact"
    """One row per logged artifact, with its tags as columns."""


class Aggregate(StrEnum):
    """How a metric with several values in one row is reduced. Saved with a chart, so a value is never renamed."""

    LATEST = "latest"
    FIRST = "first"
    MIN = "min"
    MAX = "max"
    MEAN = "mean"


class TableMode(StrEnum):
    """What a table's rows were before `TableRows`, which a table saved with it still says. Never renamed."""

    RUNS = "runs"
    PIVOT = "pivot"
    ARTIFACTS = "artifacts"


class _LegacySettings(BaseModel, frozen=True, extra="forbid"):
    """The settings a table had when it was a `mode` of one of three kinds, as they were saved."""

    mode: TableMode | None = None
    metrics: list[str] = Field(default_factory=list)
    hparams: list[str] = Field(default_factory=list)
    pivot_on: str = ""
    pivot_metric: str = ""
    artifacts: list[str] = Field(default_factory=list)
    artifact_key: str = ""
    artifact_key_is_prefix: bool = False

    def as_settings(self) -> dict[str, object]:
        """The current settings that show the same table, `show_*` being what an empty list used to mean."""
        # A table saved before modes existed was a pivot table if it said what to pivot on.
        match self.mode or (TableMode.PIVOT if self.pivot_on else TableMode.RUNS):
            case TableMode.RUNS:
                return {
                    # Naming only hyperparameters meant "hyperparameters only"; naming none meant all of both.
                    "show_metrics": self.metrics or ([] if self.hparams else ["*"]),
                    "show_hparams": self.hparams or ["*"],
                    "show_artifacts": self.artifacts,
                }
            case TableMode.PIVOT:
                if not (self.pivot_on and self.pivot_metric):
                    msg = "pivot mode needs both `pivot_on` and `pivot_metric`"
                    raise ValueError(msg)
                return {
                    "rows": TableRows.FIELD,
                    "row_field": self.pivot_on,
                    "spread_by_run": True,
                    "show_metrics": [self.pivot_metric],
                    "show_hparams": [],
                    "aggregate": Aggregate.MEAN,
                }
            case TableMode.ARTIFACTS:
                if not self.artifact_key:
                    msg = "artifacts mode needs an `artifact_key`"
                    raise ValueError(msg)
                return {
                    "rows": TableRows.ARTIFACT,
                    "show_metrics": [],
                    "show_hparams": [],
                    "show_artifacts": [f"{self.artifact_key}{'*' if self.artifact_key_is_prefix else ''}"],
                }


_LEGACY_KEYS: typing.Final = frozenset(_LegacySettings.model_fields)


class TableChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Table chart settings."""

    rows: TableRows = Field(default=TableRows.RUN)
    """What each row is: a run, a value of `row_field` (every step, say), or a logged artifact."""
    row_field: str = Field(default="")
    """Rows of a field: the metric whose values make the rows (`step`, `epoch`, ...). Unused otherwise."""
    spread_by_run: bool = Field(default=False)
    """Rows of a field: put the runs side by side, a column group each, rather than a row for every run and value. Hyperparameters are left out, a run having one value of each."""
    show_metrics: NamePatternList = Field(default_factory=lambda: ["*"])
    """Metrics to show as columns: names, or a prefix and `*` (`train/*`), `*` alone being every metric. Empty shows none."""
    show_hparams: NamePatternList = Field(default_factory=lambda: ["*"])
    """Hyperparameters to show as columns: names, or a prefix and `*` (`model.*`), `*` alone being every one. Empty shows none."""
    show_artifacts: NamePatternList = Field(default_factory=list)
    """Artifacts to show as columns: keys, or a prefix and `*` (`samples/*`), which is a column for every key under it, including ones logged later."""
    aggregate: Aggregate = Field(default=Aggregate.LATEST)
    """How a metric with several values in one row is reduced: its latest, first, min, max or mean. An artifact is always its latest."""
    page_size: typing.Annotated[int, DISPLAY_SECTION] = Field(default=20, ge=1)
    """How many rows each page of the table shows."""
    font_size: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Cell/header font size in pixels. Leave unset to use the same (smaller) default as the rest of the charts; set this to size the table up."""
    width: typing.Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to use the default flat width."""

    @model_validator(mode="before")
    @classmethod
    def _from_legacy_settings(cls, data: object) -> object:
        """A table saved with a `mode` and its own settings loads as the rows and columns that show the same table."""
        if not isinstance(data, dict):
            return data
        fields = typing.cast("dict[str, object]", data)
        legacy = {key: value for key, value in fields.items() if key in _LEGACY_KEYS}
        if not legacy:
            return data
        current = {key: value for key, value in fields.items() if key not in _LEGACY_KEYS}
        migrated = _LegacySettings.model_validate(legacy).as_settings()
        if current.keys() & migrated.keys():
            msg = f"settings saved before rows and columns can't also set {sorted(current.keys() & migrated.keys())}"
            raise ValueError(msg)
        return {**current, **migrated}

    @model_validator(mode="after")
    def _rows_have_what_they_need(self) -> typing.Self:
        match self.rows:
            case TableRows.FIELD if not self.row_field:
                msg = "rows of a field need a `row_field`"
                raise ValueError(msg)
            case TableRows.ARTIFACT if not self.show_artifacts:
                msg = "rows of artifacts need something in `show_artifacts`"
                raise ValueError(msg)
            case TableRows.RUN | TableRows.FIELD | TableRows.ARTIFACT:
                return self
