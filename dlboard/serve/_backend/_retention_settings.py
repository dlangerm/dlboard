"""What a server holds projects to: the retention rule a new project starts with, the most an owner may keep, and when."""

from __future__ import annotations

from typing import Self

import pendulum
from pydantic import NonNegativeInt, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard import models


class RetentionSettings(BaseSettings):
    """`DLBOARD_RETENTION_*` environment variables: every project's retention rule's default, its cap, and its timing."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    retention_default_max_runs: PositiveInt | None = None
    """A new project keeps this many runs of each `DLBOARD_RETENTION_DEFAULT_UNIT`. Unset, with no default age: every run."""

    retention_default_unit: models.RetentionUnit = models.RetentionUnit.EXPERIMENT
    """What `DLBOARD_RETENTION_DEFAULT_MAX_RUNS` counts over: each `experiment`'s runs, or the whole `project`'s."""

    retention_default_max_age_days: PositiveInt | None = None
    """A new project keeps runs this many days old. Not with `DLBOARD_RETENTION_DEFAULT_MAX_RUNS`."""

    retention_max_runs: PositiveInt | None = None
    """
    The most runs an owner may have each experiment keep: only an admin sets a project past it.

    With any cap set, an owner can no longer keep every run, nor pick a kind of rule that has no cap.
    """

    retention_max_age_days: PositiveInt | None = None
    """The oldest an owner may have runs kept, in days: only an admin sets a project past it."""

    retention_grace_days: NonNegativeInt = 7
    """How long a changed rule waits before it takes effect: until then the rule before it is enforced."""

    retention_run_slack: NonNegativeInt = 10
    """How many runs past its count a project or experiment may get before runs are deleted, down to the count."""

    retention_age_slack_days: NonNegativeInt = 1
    """How many days past its age a run may get before runs are deleted, down to the age."""

    retention_running_grace_days: NonNegativeInt = 7
    """
    How long a run that is still running is never deleted for. One running longer has lost its client, which then
    never said it ended: it is deleted as any other.
    """

    @model_validator(mode="after")
    def _one_default_within_the_caps(self) -> Self:
        if self.retention_default_max_runs is not None and self.retention_default_max_age_days is not None:
            msg = "set DLBOARD_RETENTION_DEFAULT_MAX_RUNS or DLBOARD_RETENTION_DEFAULT_MAX_AGE_DAYS, not both"
            raise ValueError(msg)
        if not self.within_caps(self.default_rule):
            msg = "the default retention rule (DLBOARD_RETENTION_DEFAULT_*) keeps more than its cap (DLBOARD_RETENTION_MAX_*)"
            raise ValueError(msg)
        return self

    @property
    def default_rule(self) -> models.RetentionRule:
        """
        The rule a new project starts with, and that one whose rule keeps more than the caps is lowered to.

        With no default set, the first cap: a project is never left keeping more than its cap.
        """
        if self.retention_default_max_runs is not None:
            return models.RunCount(max_runs=self.retention_default_max_runs, unit=self.retention_default_unit)
        if self.retention_default_max_age_days is not None:
            return models.RunAge(max_age_days=self.retention_default_max_age_days)
        return next(iter(self.caps), models.Unlimited())

    @property
    def caps(self) -> list[models.RetentionRule]:
        """The most each kind of rule may keep, for each kind that has a cap: a kind with none is not allowed at all."""
        caps: list[models.RetentionRule] = []
        if self.retention_max_runs is not None:
            caps.append(
                models.RunCount(max_runs=self.retention_max_runs, unit=models.RetentionUnit.EXPERIMENT)
            )
        if self.retention_max_age_days is not None:
            caps.append(models.RunAge(max_age_days=self.retention_max_age_days))
        return caps

    def within_caps(self, rule: models.RetentionRule) -> bool:
        """Whether an owner may set `rule`: with no cap, anything; else at most the cap of its kind."""
        return not self.caps or any(models.keeps_at_most(rule, cap) for cap in self.caps)

    @property
    def grace(self) -> pendulum.Duration:
        """`DLBOARD_RETENTION_GRACE_DAYS`, as a duration."""
        return pendulum.duration(days=self.retention_grace_days)
