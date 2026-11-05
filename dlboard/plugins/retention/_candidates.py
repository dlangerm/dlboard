"""
Which of a project's runs its rules say to delete now: past the slack, down to the rule, never a kept or running one.

Every read is one experiment's live runs through `get_runs`, from the newest end (a count) or the oldest (an age), and
sized by the rule: `N` and the slack, or a batch. None is sized by how many runs were logged, nor reads the Trash, which
`idx_Run_live` leaves out. How many are left to delete is counted by the database (`count_runs`), never read. A run is deleted if its `(created_at, id)` is before its experiment's cutoff: each rule that
is past its slack moves the cutoff to where it says, and the strictest wins.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pendulum

from dlboard import models
from dlboard.plugins.retention._rules import (
    RetentionRule,
    RetentionUnit,
    RunAge,
    RunCount,
    Unlimited,
    keeps_at_most,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from dlboard.models import DataStore
    from dlboard.plugins.retention._settings import RetentionSettings

type _Cutoff = tuple[datetime, int]

_OLDEST_LOOKED_AT: Final = 50
"""How many of an experiment's oldest runs an age looks through for the oldest it may delete (past ones still running)."""


@dataclass(frozen=True)
class Rules:
    """What a project is held to now: its own rule, and each experiment's own that keeps at most what it does."""

    project: RetentionRule
    experiments: Mapping[int, RetentionRule]

    def of(self, experiment_id: int) -> list[RetentionRule]:
        """The rules an experiment's runs are held to, but a count of the project's (applied to the project as a whole)."""
        own = self.experiments.get(experiment_id)
        project: list[RetentionRule] = [] if _counts_the_project(self.project) else [self.project]
        return [*project, *([own] if own is not None and keeps_at_most(own, self.project) else [])]

    @property
    def keeps_everything(self) -> bool:
        return isinstance(self.project, Unlimited) and all(
            isinstance(r, Unlimited) for r in self.experiments.values()
        )


def _counts_the_project(rule: RetentionRule) -> bool:
    return isinstance(rule, RunCount) and rule.unit is RetentionUnit.PROJECT


class Candidates:
    """The reads that find what a project's `Rules` say to delete, on `store` (as a request's user, or the system)."""

    def __init__(self, store: DataStore[...], settings: RetentionSettings, now: datetime) -> None:
        self._store = store
        self._settings = settings
        self._now = now
        self._running_grace = pendulum.duration(days=settings.retention_running_grace_days)

    def _deletable(self, run: models.Run) -> bool:
        """A run still running in its grace period is never deleted (one running longer has lost its client)."""
        return not (
            run.status is models.RunStatus.RUNNING and run.created_at > self._now - self._running_grace
        )

    def _newest(self, experiment_ids: Sequence[int], count: int) -> list[_Cutoff]:
        """The `count` newest runs of these experiments that a count counts (not kept), newest first, merged."""
        per_experiment = [
            [
                (r.created_at, r.id)
                for r in self._store.get_runs(e, limit=count, run_ids=None, include_kept=False)
            ]
            for e in experiment_ids
        ]
        return list(heapq.merge(*per_experiment, reverse=True))[:count]

    def _count_cutoff(self, experiment_ids: Sequence[int], max_runs: int) -> _Cutoff | None:
        """Where a count cuts these experiments' runs, once they hold its slack past it: at its last run kept."""
        slack = self._settings.retention_run_slack
        if len(experiment_ids) == 1:
            # Two probes whatever the count: the run past the slack, and the last one kept, found by position (the
            # database walks the index, and ships one run each) rather than by reading all of what a project keeps.
            (experiment_id,) = experiment_ids

            def at(position: int) -> models.Run | None:
                runs = self._store.get_runs(
                    experiment_id, limit=1, offset=position, run_ids=None, include_kept=False
                )
                return next(iter(runs), None)

            last = at(max_runs - 1) if at(max_runs + slack) is not None else None
            return (last.created_at, last.id) if last is not None else None
        held = self._newest(experiment_ids, max_runs + slack + 1)
        return held[max_runs - 1] if len(held) > max_runs + slack else None

    def _age_cutoff(self, experiment_id: int, max_age_days: int) -> _Cutoff | None:
        """Where an age cuts an experiment's runs, once its oldest deletable run is its slack past it."""
        cutoff = self._now - pendulum.duration(days=max_age_days)
        slack = pendulum.duration(days=self._settings.retention_age_slack_days)
        oldest = next(
            (
                r
                for r in self._store.get_runs(
                    experiment_id,
                    limit=_OLDEST_LOOKED_AT,
                    run_ids=None,
                    oldest_first=True,
                    include_kept=False,
                )
                if self._deletable(r)
            ),
            None,
        )
        return (cutoff, 0) if oldest is not None and oldest.created_at < cutoff - slack else None

    def _cutoff(self, experiment_id: int, rule: RetentionRule) -> _Cutoff | None:
        match rule:
            case Unlimited():
                return None
            case RunCount(
                max_runs=max_runs
            ):  # an experiment's own count is of its own runs, whatever its unit
                return self._count_cutoff([experiment_id], max_runs)
            case RunAge(max_age_days=max_age_days):
                return self._age_cutoff(experiment_id, max_age_days)

    def cutoffs(self, project_id: int, rules: Rules) -> dict[int, _Cutoff]:
        """
        Where each experiment's runs are cut under `rules`: the strictest cutoff of the rules past their slack.

        A check works this out once, then reads and counts against it batch after batch: deleting what is past a cutoff
        moves no cutoff. (A run kept meanwhile moves one a place, which the next check sees.)
        """
        if rules.keeps_everything:
            return {}
        experiment_ids = [e.id for e in self._store.get_experiments(project_id)]
        cutoffs: dict[int, _Cutoff] = {}
        if (
            isinstance(rules.project, RunCount)
            and _counts_the_project(rules.project)
            and (whole := self._count_cutoff(experiment_ids, rules.project.max_runs)) is not None
        ):
            cutoffs = dict.fromkeys(experiment_ids, whole)
        for experiment_id in experiment_ids:
            for cutoff in (self._cutoff(experiment_id, rule) for rule in rules.of(experiment_id)):
                if cutoff is not None:
                    cutoffs[experiment_id] = max(cutoffs.get(experiment_id, cutoff), cutoff)
        return cutoffs

    def read(self, cutoffs: dict[int, _Cutoff], *, limit: int) -> list[int]:
        """
        The runs past `cutoffs` to delete next, oldest first within an experiment: at most `limit`.

        Any run past its experiment's cutoff is as good to delete as another, so experiments are read in turn, each for
        only what is still wanted, and a batch reads about `limit` runs however many experiments there are. An
        experiment with nothing more to delete is dropped from `cutoffs`, so the next batch does not ask it again.
        """
        found: list[_Cutoff] = []
        for experiment_id, cut in list(cutoffs.items()):
            wanted = limit - len(found)
            if wanted <= 0:
                break
            asked = wanted + _OLDEST_LOOKED_AT
            older = list(
                self._store.get_runs(
                    experiment_id,
                    limit=asked,
                    run_ids=None,
                    created_before=cut[0] + pendulum.duration(microseconds=1),
                    oldest_first=True,
                    include_kept=False,
                )
            )
            deletable = [
                (r.created_at, r.id) for r in older if (r.created_at, r.id) < cut and self._deletable(r)
            ]
            found.extend(deletable)
            if len(older) < asked and len(deletable) <= wanted:
                del cutoffs[experiment_id]  # all of it was read, and all of it is in this batch
        return [run_id for _, run_id in sorted(found)[:limit]]

    def count(self, cutoffs: dict[int, _Cutoff], *, limit: int) -> int:
        """
        How many runs are past `cutoffs`, up to `limit`: what `read` would find, counted by the database.

        One bounded count an experiment past its cutoff, none of the runs read, so it costs what the project has
        experiments, not what it has runs to delete.
        """
        total = 0
        for experiment_id, cut in cutoffs.items():
            total += self._store.count_runs(
                experiment_id,
                before=cut,
                include_kept=False,
                spare_running_after=self._now - self._running_grace,
                limit=limit - total,
            )
            if total >= limit:
                break
        return total

    def find(self, project_id: int, rules: Rules, *, limit: int) -> list[int]:
        """The first batch `rules` say to delete (a check reads batch after batch from `cutoffs` instead)."""
        return self.read(self.cutoffs(project_id, rules), limit=limit)

    def overdue(self, project_id: int, rules: Rules, *, limit: int) -> int:
        """How many runs `rules` say to delete now, up to `limit`: for a preview, which has no batches to read."""
        return self.count(self.cutoffs(project_id, rules), limit=limit)
