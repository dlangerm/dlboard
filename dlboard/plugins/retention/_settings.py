"""What a server holds projects to: the retention rule a new project starts with, the most an owner may keep, and when."""

from __future__ import annotations

from typing import Final, Self

import pendulum
from pydantic import NonNegativeInt, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlboard.plugins.retention._rules import (
    RetentionRule,
    RetentionUnit,
    RunAge,
    RunCount,
    Unlimited,
    keeps_at_most,
)

OVERDUE_COUNTED: Final = 10_000
"""How far a project's overdue runs are counted: past it the status says "or more", so a count never scans a backlog."""


class RetentionSettings(BaseSettings):
    """`DLBOARD_RETENTION_*` environment variables: every project's retention rule's default, its cap, and its timing."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    retention_default_max_runs: PositiveInt | None = None
    """A new project keeps this many runs of each `DLBOARD_RETENTION_DEFAULT_UNIT`. Unset, with no default age: every run."""

    retention_default_unit: RetentionUnit = RetentionUnit.EXPERIMENT
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

    retention_warn_after_hours: PositiveInt = 24
    """How long a project may hold runs its rule says to delete before the project page says it is out of spec."""

    retention_lock_after_hours: PositiveInt | None = None
    """
    How long a project may hold runs its rule says to delete before it takes no new runs (the REST API refuses them) until
    retention catches up, so that a project cannot be filled faster than it is drained. Unset, a project is only warned.
    Runs already logging are never refused. At least `retention_warn_after_hours`.
    """

    retention_interval_s: PositiveInt = 300
    """How often the custodian looks for projects whose rule needs checking."""

    retention_batch: PositiveInt = 100
    """The most runs one check deletes in a batch: the custodian paces itself between batches."""

    retention_project_budget: PositiveInt = 5000
    """The most runs one check of a project deletes before it moves on, so one huge backlog cannot starve the others."""

    @model_validator(mode="after")
    def _one_default_within_the_caps(self) -> Self:
        if self.retention_default_max_runs is not None and self.retention_default_max_age_days is not None:
            msg = "set DLBOARD_RETENTION_DEFAULT_MAX_RUNS or DLBOARD_RETENTION_DEFAULT_MAX_AGE_DAYS, not both"
            raise ValueError(msg)
        if (
            self.retention_lock_after_hours is not None
            and self.retention_lock_after_hours < self.retention_warn_after_hours
        ):
            msg = "DLBOARD_RETENTION_LOCK_AFTER_HOURS is at least DLBOARD_RETENTION_WARN_AFTER_HOURS: a project is warned before it is locked"
            raise ValueError(msg)
        if not self.within_caps(self.default_rule):
            msg = "the default retention rule (DLBOARD_RETENTION_DEFAULT_*) keeps more than its cap (DLBOARD_RETENTION_MAX_*)"
            raise ValueError(msg)
        return self

    @property
    def default_rule(self) -> RetentionRule:
        """
        The rule a new project starts with, and that one whose rule keeps more than the caps is lowered to.

        With no default set, the first cap: a project is never left keeping more than its cap.
        """
        if self.retention_default_max_runs is not None:
            return RunCount(max_runs=self.retention_default_max_runs, unit=self.retention_default_unit)
        if self.retention_default_max_age_days is not None:
            return RunAge(max_age_days=self.retention_default_max_age_days)
        return next(iter(self.caps), Unlimited())

    @property
    def caps(self) -> list[RetentionRule]:
        """The most each kind of rule may keep, for each kind that has a cap: a kind with none is not allowed at all."""
        caps: list[RetentionRule] = []
        if self.retention_max_runs is not None:
            caps.append(RunCount(max_runs=self.retention_max_runs, unit=RetentionUnit.EXPERIMENT))
        if self.retention_max_age_days is not None:
            caps.append(RunAge(max_age_days=self.retention_max_age_days))
        return caps

    def within_caps(self, rule: RetentionRule) -> bool:
        """Whether an owner may set `rule`: with no cap, anything; else at most the cap of its kind."""
        return not self.caps or any(keeps_at_most(rule, cap) for cap in self.caps)

    @property
    def lock_after(self) -> pendulum.Duration | None:
        """`DLBOARD_RETENTION_LOCK_AFTER_HOURS` as a duration, if a lock is set."""
        hours = self.retention_lock_after_hours
        return pendulum.duration(hours=hours) if hours is not None else None

    @property
    def grace(self) -> pendulum.Duration:
        """`DLBOARD_RETENTION_GRACE_DAYS`, as a duration."""
        return pendulum.duration(days=self.retention_grace_days)
