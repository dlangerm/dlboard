"""
What a retention rule keeps, and how a saved one waits out its grace period: the plugin's own JSON, in a scoped setting.

A project's (or experiment's) setting holds the rule in effect and, after a change, the one pending, which takes effect
once its grace period is over. Which one is in effect is worked out from the time whenever it is read, so nothing has to
happen when a grace period ends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, PositiveInt

from dlboard._compat import StrEnum

if TYPE_CHECKING:
    from datetime import datetime

    import pendulum


class RetentionKey(StrEnum):
    """The plugin's scoped settings. Stored as keys, so a value is never renamed or removed."""

    RULE = "retention.rule"
    """A project's or experiment's rule: `RetentionSetting`."""
    STATUS = "retention.status"
    """How a project stood when last checked: `RetentionStatus`. The worker's to write."""


class RetentionKind(StrEnum):
    """What a rule bounds. Stored in the rule's JSON, so a value is never renamed or removed."""

    UNLIMITED = "unlimited"
    RUN_COUNT = "run_count"
    RUN_AGE = "run_age"


class RetentionUnit(StrEnum):
    """What a count of runs is counted over. Stored in the rule's JSON, so a value is never renamed or removed."""

    PROJECT = "project"
    """Every run of the project together: the newest that many, whichever experiments they are of."""
    EXPERIMENT = "experiment"
    """Each experiment's runs on their own."""


class Unlimited(BaseModel, frozen=True, extra="forbid"):
    """Keep every run."""

    kind: Literal[RetentionKind.UNLIMITED] = RetentionKind.UNLIMITED


class RunCount(BaseModel, frozen=True, extra="forbid"):
    """Keep the newest `max_runs` runs of each `unit`."""

    kind: Literal[RetentionKind.RUN_COUNT] = RetentionKind.RUN_COUNT
    max_runs: PositiveInt
    unit: RetentionUnit = RetentionUnit.EXPERIMENT


class RunAge(BaseModel, frozen=True, extra="forbid"):
    """Keep the runs created in the last `max_age_days` days."""

    kind: Literal[RetentionKind.RUN_AGE] = RetentionKind.RUN_AGE
    max_age_days: PositiveInt


type RetentionRule = Annotated[Unlimited | RunCount | RunAge, Field(discriminator="kind")]
"""What a project or experiment keeps."""


def keeps_at_most(rule: RetentionRule, ceiling: RetentionRule) -> bool:
    """
    Whether `rule` keeps no more than `ceiling` does: never a rule of another kind, unless `ceiling` keeps everything.

    A count of each experiment's runs keeps more than the same count of the whole project's, so it is never at most that.
    """
    match ceiling, rule:
        case Unlimited(), _:
            return True
        case RunCount(), RunCount():
            fewer_units = rule.unit is RetentionUnit.PROJECT or ceiling.unit is RetentionUnit.EXPERIMENT
            return rule.max_runs <= ceiling.max_runs and fewer_units
        case RunAge(), RunAge():
            return rule.max_age_days <= ceiling.max_age_days
        case _:
            return False


_COUNTED_OVER = {RetentionUnit.EXPERIMENT: "each experiment", RetentionUnit.PROJECT: "the project"}


def describe(rule: RetentionRule) -> str:
    """A rule, in words."""
    match rule:
        case Unlimited():
            return "Keeps every run"
        case RunCount(max_runs=n, unit=unit):
            return f"Keeps the newest {n} runs of {_COUNTED_OVER[unit]}"
        case RunAge(max_age_days=days):
            return f"Keeps runs from the last {days} day{'s' if days != 1 else ''}"


class RuleVersion(BaseModel, frozen=True, extra="forbid"):
    """One saved rule: who saved it, and from when it is in effect."""

    rule: RetentionRule
    set_by: int | None = None
    set_at: AwareDatetime
    effective_at: AwareDatetime
    cap_exempt: bool = False
    """Whether an admin set it past the server's cap (`DLBOARD_RETENTION_MAX_*`), which then never lowers it."""


class RetentionSetting(BaseModel, frozen=True, extra="forbid"):
    """A scope's `RetentionKey.RULE`: the rule it was held to, and the one it will be once its grace period is over."""

    active: RuleVersion | None = None
    pending: RuleVersion | None = None

    def as_of(self, now: datetime) -> RetentionSetting:
        """The setting as it stands at `now`: a pending rule whose grace period is over is the one in effect."""
        if self.pending is not None and self.pending.effective_at <= now:
            return RetentionSetting(active=self.pending)
        return self

    def in_effect(self, now: datetime) -> RetentionRule | None:
        """The rule in effect at `now`, if this scope has one."""
        active = self.as_of(now).active
        return active.rule if active is not None else None

    def latest(self) -> RuleVersion | None:
        """The rule this scope will be held to: the pending one, else the one in effect."""
        return self.pending or self.active

    def saved(
        self,
        rule: RetentionRule,
        *,
        by: int | None,
        now: datetime,
        grace: pendulum.Duration,
        cap_exempt: bool = False,
    ) -> RetentionSetting:
        """
        The setting once `rule` is saved: pending until its grace period is over, while the rule in effect is enforced.

        Saving another rule replaces the pending one, never the one in effect, so saving over and over never puts
        retention off; saving the rule already pending keeps its time, so it is not put off either, unless the grace
        period is shorter now than when it was first saved, which moves it earlier; saving the rule in effect drops
        the pending one.
        """
        current = self.as_of(now)
        if current.active is not None and current.active.rule == rule:
            return RetentionSetting(active=current.active)
        if current.pending is not None and current.pending.rule == rule:
            effective_at = min(current.pending.effective_at, now + grace)
            version = current.pending.model_copy(
                update={"cap_exempt": cap_exempt, "effective_at": effective_at}
            )
        else:
            version = RuleVersion(
                rule=rule, set_by=by, set_at=now, effective_at=now + grace, cap_exempt=cap_exempt
            )
        return RetentionSetting(active=current.active, pending=version)

    def dump(self) -> dict[str, Any]:
        """As the scoped setting stores it."""
        return self.model_dump(mode="json")


class RetentionStatus(BaseModel, frozen=True, extra="forbid"):
    """A project's `RetentionKey.STATUS`: how it stood against its rules when the worker last checked it."""

    checked_at: AwareDatetime
    overdue_runs: int = 0
    """How many runs it still holds that its rules say to delete (counted up to `OVERDUE_COUNTED`)."""
    out_of_spec_since: AwareDatetime | None = None
    """Since when it has held runs to delete; `None` while it holds none."""

    def is_locked(self, now: datetime, after: pendulum.Duration | None) -> bool:
        """Whether it has held runs to delete for `after` (if a lock is set): it then takes no new runs until it catches up."""
        return (
            after is not None and self.out_of_spec_since is not None and now - self.out_of_spec_since >= after
        )
