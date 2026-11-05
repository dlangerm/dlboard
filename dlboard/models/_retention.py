"""
Retention: how many runs, or how old, a project or one of its experiments keeps before older runs are deleted.

A project has one rule, and each of its experiments may have a stricter one of the same kind. A rule is saved as a
policy that takes effect after a grace period (`DLBOARD_RETENTION_GRACE_DAYS`); the one in effect until then goes on
being enforced. A kept run (`Run.kept_at`) is never deleted by retention, and only a project's owner deletes one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, Field, PositiveInt, TypeAdapter

from dlboard._compat import StrEnum

if TYPE_CHECKING:
    from typing_extensions import Self


class RetentionKind(StrEnum):
    """
    What a retention rule bounds.

    Written to a database column as the plain string value, so a value is never renamed or removed -- see
    `docs/compatibility.md`.
    """

    UNLIMITED = "unlimited"
    """Nothing: every run is kept."""
    RUN_COUNT = "run_count"
    """How many runs: the oldest beyond that many are deleted."""
    RUN_AGE = "run_age"
    """How old a run is: one created longer ago than that is deleted."""


class RetentionUnit(StrEnum):
    """
    What a count of runs is counted over. Written to a database column, so a value is never renamed or removed.

    An age needs no unit: how old a run is does not depend on what else is counted with it.
    """

    PROJECT = "project"
    """Every run of the project, together: the newest that many are kept, whichever experiments they are of."""
    EXPERIMENT = "experiment"
    """Each experiment's runs on their own: every experiment keeps its newest that many."""


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


RetentionRule: TypeAlias = Annotated[Unlimited | RunCount | RunAge, Field(discriminator="kind")]
"""What a project or experiment keeps."""

RETENTION_RULE: TypeAdapter[RetentionRule] = TypeAdapter(RetentionRule)


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


class NewRetentionPolicy(BaseModel, frozen=True, extra="forbid"):
    """
    A rule a project (or one of its experiments) is held to from `effective_at` on, as one row.

    The rule's fields are columns of their own, `None` where its kind has no such field: read it as `rule`.
    """

    project_id: int
    experiment_id: int | None = None
    """The experiment the rule is stricter for, or `None` for the project's own rule."""

    kind: RetentionKind
    max_runs: int | None = None
    unit: RetentionUnit | None = None
    max_age_days: int | None = None

    cap_exempt: bool = False
    """Whether an admin set this rule past the server's cap (`DLBOARD_RETENTION_MAX_*`), which then never lowers it."""

    set_by: int | None = None
    set_at: AwareDatetime
    effective_at: AwareDatetime
    """When it takes effect: until then the rule before it is enforced."""

    @classmethod
    def of(  # noqa: PLR0913 -- a row's every column but its id
        cls,
        rule: RetentionRule,
        *,
        project_id: int,
        experiment_id: int | None,
        set_by: int | None,
        set_at: AwareDatetime,
        effective_at: AwareDatetime,
        cap_exempt: bool = False,
    ) -> Self:
        """The row holding `rule`, every column its kind has no field for `None`."""
        return cls.model_validate(
            {
                "max_runs": None,
                "unit": None,
                "max_age_days": None,
                **rule.model_dump(),
                "project_id": project_id,
                "experiment_id": experiment_id,
                "cap_exempt": cap_exempt,
                "set_by": set_by,
                "set_at": set_at,
                "effective_at": effective_at,
            }
        )

    @property
    def rule(self) -> RetentionRule:
        """The rule this row stores."""
        return RETENTION_RULE.validate_python(
            self.model_dump(include={"kind", "max_runs", "unit", "max_age_days"}, exclude_none=True)
        )


class RetentionPolicy(NewRetentionPolicy, frozen=True, extra="forbid"):
    """A stored retention policy."""

    id: int


class ScopedRetention(BaseModel, frozen=True, extra="forbid"):
    """The policy a project or experiment is held to now, and the one it will be once its grace period is over."""

    active: RetentionPolicy | None = None
    """`None` for an experiment with no rule of its own (it follows its project's), or a project from before retention."""
    pending: RetentionPolicy | None = None


class RetentionStatus(BaseModel, frozen=True, extra="forbid"):
    """How a project stood against its retention policies when the retention worker last checked it."""

    project_id: int
    checked_at: AwareDatetime
    overdue_runs: int = 0
    """How many runs it still holds that its policies say to delete (counted only so far: see the worker)."""
    out_of_spec_since: AwareDatetime | None = None
    """Since when it has held runs to delete that the worker has not yet deleted; `None` while it holds none."""

    id: int


class ProjectRetention(BaseModel, frozen=True, extra="forbid"):
    """A project's retention policies, its experiments' own, and how it stood when last checked."""

    project: ScopedRetention
    experiments: dict[int, ScopedRetention] = {}
    """Only the experiments with a rule of their own (in effect or pending)."""
    status: RetentionStatus | None = None


class RetentionWorkerState(BaseModel, frozen=True, extra="forbid"):
    """Which server process enforces retention, and when it last swept: for the admin page."""

    lease_holder: str | None = None
    """The process holding the lease (`host:pid:tag`), while it does; `None` before any has swept."""
    lease_until: AwareDatetime | None = None
    swept_at: AwareDatetime | None = None
    """When every project was last checked."""
    trash_retention_since: AwareDatetime | None = None
    """When retention first swept this database: the Trash is purged counting from no earlier than this."""
