# pyright: reportPrivateUsage=false
"""Tests for `_cascade_targets`: the generic ownership-graph walk soft-delete/restore/purge use.

These are pure unit tests against the graph-walking function itself -- no database involved --
covering the two things a hand-typed cascade list can't easily prove: that a new owned table
automatically participates with no extra code, and that `within` actually prunes (not just stops
recursing past) tables outside it.
"""

from __future__ import annotations

from dltrack import models
from dltrack.serve._backend import _sql_store_base as store_base


def _table_names(targets: list[store_base.CascadePath]) -> list[str]:
    return [path[-1][0].__name__ for path in targets]


def test_cascade_targets_from_project_unfiltered_includes_every_owned_descendant() -> None:
    targets = store_base._cascade_targets(models.Project)

    names = _table_names(targets)
    assert names.count("Experiment") == 1
    assert names.count("Run") == 1
    assert names.count("UnderlyingMetricTableEntry") == 1
    assert names.count("HyperParams") == 1
    assert names.count("Artifact") == 1
    # Page is reachable from Project via three separate ownership edges (project_id directly,
    # experiment_id under Project's experiments, run_id under those experiments' runs).
    assert names.count("Page") == 3


def test_cascade_targets_within_soft_deletable_excludes_non_soft_deletable_tables() -> None:
    targets = store_base._cascade_targets(models.Project, within=store_base.SOFT_DELETABLE)

    names = _table_names(targets)
    assert set(names) == {"Experiment", "Run", "Artifact"}
    assert "Page" not in names, "Page has no deleted_at -- must never appear in a soft-delete cascade"
    assert "UnderlyingMetricTableEntry" not in names
    assert "HyperParams" not in names


def test_cascade_targets_within_soft_deletable_from_run_is_just_artifact() -> None:
    targets = store_base._cascade_targets(models.Run, within=store_base.SOFT_DELETABLE)
    assert _table_names(targets) == ["Artifact"]


def test_cascade_targets_from_artifact_is_empty_leaf() -> None:
    assert store_base._cascade_targets(models.Artifact) == []


def test_cascade_targets_paths_nest_through_ancestors() -> None:
    targets = {
        path[-1][0].__name__: path
        for path in store_base._cascade_targets(models.Project, within=store_base.SOFT_DELETABLE)
    }

    experiment = (models.Experiment, "project_id")
    run = (models.Run, "experiment_id")
    assert targets["Experiment"] == (experiment,)
    assert targets["Run"] == (experiment, run)
    assert targets["Artifact"] == (experiment, run, (models.Artifact, "run_id"))


def test_a_newly_registered_owned_table_participates_with_no_other_code_change() -> None:
    """
    Proves the actual scaling claim: registering a fake ownership edge is enough on its own.

    Temporarily adds a throwaway table to `FOREIGN_KEYS` as an `Experiment`-owned child and
    confirms `_cascade_targets` picks it up immediately -- nothing else needs to change for a real
    new model to participate in soft-delete/restore cascades and purge's audit-log counts.
    """
    from pydantic import BaseModel

    from dltrack.serve._backend._foreign_keys import ForeignKey, ForeignKeyKind

    class _FakeChild(BaseModel, frozen=True, extra="forbid"):
        id: int
        experiment_id: int

    original = dict(store_base.FOREIGN_KEYS)
    try:
        store_base.FOREIGN_KEYS[_FakeChild] = {
            "experiment_id": ForeignKey(models.Experiment, ForeignKeyKind.OWNERSHIP)
        }
        names = _table_names(store_base._cascade_targets(models.Project))
        assert "_FakeChild" in names
    finally:
        store_base.FOREIGN_KEYS.clear()
        store_base.FOREIGN_KEYS.update(original)
