# pyright: reportPrivateUsage=false
"""End-to-end tests against real data stores, on every backend.

Covers the table mapping in `_sql.py` and the CRUD flows in `SQLStoreBase` together, against
real databases rather than mocks -- the `store` fixture is sqlite and Postgres in turn.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from dlboard import models
from dlboard.conftest import EVERY_STORE_BACKEND, StoreBackend
from dlboard.models._view import PanelInstance
from dlboard.serve._backend._metric_frame import MetricKeySummary
from dlboard.serve._pages._experiment._experiment_page_state import BasicExperimentPage

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(params=EVERY_STORE_BACKEND)
def store_backend(request: pytest.FixtureRequest) -> StoreBackend:
    return request.param


def _count(store: SQLLiteStore, model: type[BaseModel], *where: sa.ColumnElement[bool]) -> int:
    ((count,),) = store._execute(sa.select(sa.func.count()).select_from(store.tables[model]).where(*where))
    return count


def test_project_and_experiment_are_persisted(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    assert store.get_project(project.id) == project

    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    assert store.get_experiment(experiment.id) == experiment
    assert list(store.get_experiments(project.id)) == [experiment]
    assert store.get_experiment(experiment.id + 1000) is None


def test_create_experiment_stores_name_and_description(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(
        models.NewExperiment(project_id=project.id, name="my-exp", description="does a thing")
    )

    assert experiment.name == "my-exp"
    assert experiment.description == "does a thing"
    assert store.get_experiment(experiment.id) == experiment


def test_create_experiment_defaults_name_and_description_to_empty(store: SQLLiteStore) -> None:
    """The CLI logger creates experiments without a name/description; both must stay optional."""
    project = store.create_project(models.NewProject(name="p", description="d"))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id))

    assert experiment.name == ""
    assert experiment.description == ""


def test_update_project_persists_description_change(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="old"))

    updated = store.update_project(project.model_copy(update={"description": "new"}))

    assert updated.id == project.id
    assert updated.description == "new"
    assert store.get_project(project.id) == updated


def test_update_experiment_persists_name_and_description_change(
    store: SQLLiteStore, experiment_id: int
) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None

    updated = store.update_experiment(
        experiment.model_copy(update={"name": "renamed", "description": "new description"})
    )

    assert updated.id == experiment_id
    assert updated.name == "renamed"
    assert updated.description == "new description"
    refetched = store.get_experiment(experiment_id)
    assert refetched is not None
    assert (refetched.name, refetched.description) == ("renamed", "new description")


def test_get_runs_paginates_most_recently_created_first(store: SQLLiteStore, experiment_id: int) -> None:
    runs = [store.create_run(models.NewRun(experiment_id=experiment_id, name=f"run-{i}")) for i in range(3)]

    first_page = list(store.get_runs(experiment_id, limit=2, offset=0))
    second_page = list(store.get_runs(experiment_id, limit=2, offset=2))

    assert [r.id for r in first_page] == [runs[2].id, runs[1].id]
    assert [r.id for r in second_page] == [runs[0].id]


def test_a_new_run_is_running_and_finishing_it_records_how_and_when(
    store: SQLLiteStore, experiment_id: int
) -> None:
    created = store.create_run(models.NewRun(experiment_id=experiment_id))
    assert (created.status, created.ended_at) == (models.RunStatus.RUNNING, None)
    ended_at = datetime(2026, 1, 1, 12, tzinfo=UTC)

    finished = store.finish_run(created.id, models.RunStatus.FAILED, ended_at)

    assert (finished.status, finished.ended_at) == (models.RunStatus.FAILED, ended_at)
    assert store.get_run(created.id) == finished


def test_finishing_a_run_again_keeps_the_newest_report(store: SQLLiteStore, experiment_id: int) -> None:
    """Lightning finalizes after `fit` and again after `test`."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    first = datetime(2026, 1, 1, 12, tzinfo=UTC)
    store.finish_run(run.id, models.RunStatus.FINISHED, first)

    store.finish_run(run.id, models.RunStatus.FAILED, first + timedelta(minutes=5))

    latest = store.get_run(run.id)
    assert latest is not None
    assert (latest.status, latest.ended_at) == (models.RunStatus.FAILED, first + timedelta(minutes=5))


def test_finishing_a_run_bumps_its_experiments_revision_so_a_live_page_notices(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    before = store.get_experiment(experiment_id)
    assert before is not None

    store.finish_run(run.id, models.RunStatus.FINISHED, datetime(2026, 1, 1, tzinfo=UTC))

    after = store.get_experiment(experiment_id)
    assert after is not None
    assert after.revision > before.revision


def test_a_run_stored_before_status_existed_reads_back_with_none(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    runs = store._tables[models.Run]
    store._execute(sa.update(runs).where(runs.c.id == run.id).values(status=None))

    legacy = store.get_run(run.id)

    assert legacy is not None
    assert (legacy.status, legacy.ended_at) == (None, None)


def test_finishing_a_missing_or_deleted_run_raises(store: SQLLiteStore, experiment_id: int) -> None:
    deleted = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(deleted.id, store.get_or_create_user(models.Principal.unverified("alice")))

    for run_id in (deleted.id, 999_999):
        with pytest.raises(ValueError, match=r"deleted|does not exist"):
            store.finish_run(run_id, models.RunStatus.FINISHED, datetime(2026, 1, 1, tzinfo=UTC))


def test_a_run_created_without_a_name_gets_a_generated_one_that_survives_persistence(
    store: SQLLiteStore, experiment_id: int
) -> None:
    created = store.create_run(models.NewRun(experiment_id=experiment_id))

    assert created.name
    refetched = store.get_run(created.id)
    assert refetched is not None
    assert refetched.name == created.name


def test_get_runs_defaults_to_a_generous_page_covering_typical_use(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    assert list(store.get_runs(experiment_id)) == [run]


def test_get_runs_excludes_deleted_and_other_experiments(store: SQLLiteStore, experiment_id: int) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    store.create_run(models.NewRun(experiment_id=other_experiment.id))

    actor = store.get_or_create_user(models.Principal.unverified("alice"))
    kept = store.create_run(models.NewRun(experiment_id=experiment_id))
    deleted = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.delete_run(deleted.id, actor)

    runs = list(store.get_runs(experiment_id, limit=100, offset=0))

    assert [r.id for r in runs] == [kept.id]


def test_log_hyperparams_skips_duplicate_run(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    first = store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    second = store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.2}))

    assert second.id == first.id
    assert [h.id for h in store.fetch_hyperparams(experiment_id)] == [first.id]


def _log_step(
    store: SQLLiteStore, run: models.Run, step: int, ts: datetime = _TS, **metrics: float | None
) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics=metrics, step=step, experiment_id=run.experiment_id, run_id=run.id, timestamp_utc=ts
            )
        ]
    )


def test_fetch_metrics_pivots_to_one_row_per_run_and_step(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_step(store, run, 0, loss=0.5)
    _log_step(store, run, 1, loss=0.4, acc=0.9)

    frame = store.fetch_metrics(experiment_id)

    assert frame.keys == {"loss", "acc"}
    assert list(frame.step) == [0, 1]
    assert list(frame.values("loss")) == [0.5, 0.4]
    assert frame.values("acc").isna().tolist() == [True, False]
    assert store.fetch_metrics(experiment_id, keys=frozenset({"acc"})).keys == {"acc"}


def test_fetch_metrics_stamps_each_step_with_its_own_timestamp(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Used to stamp every step with the *next* step's timestamp, shifting date/time x-axes by one."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    later = _TS + timedelta(minutes=1)
    _log_step(store, run, 0, _TS, loss=0.5)
    _log_step(store, run, 1, later, loss=0.4)

    assert list(store.fetch_metrics(experiment_id).timestamp_utc) == [_TS, later]


def test_fetch_metrics_keeps_the_last_value_written_for_a_key_relogged_at_one_step(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Which duplicate won used to be whatever order sqlite's sort happened to emit ties in."""
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_step(store, run, 0, loss=1.0)
    _log_step(store, run, 0, loss=2.0)

    assert list(store.fetch_metrics(experiment_id).values("loss")) == [2.0]


def test_summarize_metric_keys_counts_each_keys_steps_per_run(
    store: SQLLiteStore, experiment_id: int
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_step(store, run, 0, loss=0.5, acc=0.1)
    _log_step(store, run, 1, loss=0.4)

    assert store.summarize_metric_keys(experiment_id) == [
        MetricKeySummary("acc", 1),
        MetricKeySummary("loss", 2),
    ]


def test_summarize_metric_keys_excludes_deleted_runs_and_other_experiments(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))
    _log_step(store, store.create_run(models.NewRun(experiment_id=other_experiment.id)), 0, other_metric=1.0)
    deleted_run = store.create_run(models.NewRun(experiment_id=experiment_id))
    _log_step(store, deleted_run, 0, deleted_run_metric=1.0)
    store.delete_run(deleted_run.id, store.get_or_create_user(models.Principal.unverified("alice")))

    assert store.summarize_metric_keys(experiment_id) == []


def test_log_and_fetch_artifacts_decodes_tags(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img",
                fname="img.png",
                run_id=run.id,
                experiment_id=experiment_id,
                step=0,
                ref="ref://a",
                tags={"split": "train"},
            ),
            models.Artifact(
                key="other",
                fname="o.bin",
                run_id=run.id,
                experiment_id=experiment_id,
                step=0,
                ref="ref://b",
            ),
        ]
    )

    fetched = list(store.fetch_artifacts(experiment_id=experiment_id, keys=frozenset({"img"})))
    assert len(fetched) == 1
    assert fetched[0].tags == {"split": "train"}
    assert fetched[0].ref == "ref://a"


@pytest.mark.parametrize(
    ("keys", "key_prefixes", "expected"),
    [
        pytest.param(None, frozenset[str](), {"ckpt/a", "ckpt/b", "ckpt_other", "img"}, id="everything"),
        pytest.param(frozenset({"img"}), frozenset[str](), {"img"}, id="exact-key"),
        pytest.param(None, frozenset({"ckpt/"}), {"ckpt/a", "ckpt/b"}, id="prefix"),
        pytest.param(frozenset({"img"}), frozenset({"ckpt/"}), {"ckpt/a", "ckpt/b", "img"}, id="both"),
        pytest.param(frozenset[str](), frozenset[str](), set[str](), id="no-keys"),
        pytest.param(None, frozenset({"ck_t"}), set[str](), id="wildcards-are-literal"),
    ],
)
def test_fetch_artifacts_by_key_and_key_prefix(
    store: SQLLiteStore,
    experiment_id: int,
    keys: frozenset[str] | None,
    key_prefixes: frozenset[str],
    expected: set[str],
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_artifact_refs(
        [
            models.Artifact(
                key=key, fname=key, run_id=run.id, experiment_id=experiment_id, step=0, ref=f"ref://{key}"
            )
            for key in ("ckpt/a", "ckpt/b", "ckpt_other", "img")
        ]
    )

    fetched = store.fetch_artifacts(experiment_id, keys=keys, key_prefixes=key_prefixes)

    assert {a.key for a in fetched} == expected


def test_fetches_leave_out_excluded_runs(store: SQLLiteStore, experiment_id: int) -> None:
    kept, excluded = (store.create_run(models.NewRun(experiment_id=experiment_id)) for _ in range(2))
    for run in (kept, excluded):
        _log_step(store, run, 0, loss=1.0)
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
        store.log_artifact_refs(
            [
                models.Artifact(
                    key="img",
                    fname="i.png",
                    run_id=run.id,
                    experiment_id=experiment_id,
                    step=0,
                    ref="ref://a",
                )
            ]
        )
    skip = frozenset({excluded.id})

    assert set(store.fetch_metrics(experiment_id, exclude_run_ids=skip).run_id) == {kept.id}
    assert {h.run_id for h in store.fetch_hyperparams(experiment_id, exclude_run_ids=skip)} == {kept.id}
    assert {a.run_id for a in store.fetch_artifacts(experiment_id, exclude_run_ids=skip)} == {kept.id}


@pytest.mark.parametrize(
    ("run_id", "experiment_id_", "project_id"),
    [
        (None, None, None),
        (1, 1, None),
    ],
)
def test_get_or_create_page_requires_exactly_one_id(
    store: SQLLiteStore, run_id: int | None, experiment_id_: int | None, project_id: int | None
) -> None:
    with pytest.raises(ValueError, match="Exactly one"):
        store.get_or_create_page(
            BasicExperimentPage, run_id=run_id, experiment_id=experiment_id_, project_id=project_id
        )


def test_get_or_create_user_grants_bootstrap_scopes_to_the_first_user_only(store: SQLLiteStore) -> None:
    first = store.get_or_create_user(models.Principal.unverified("alice"))
    second = store.get_or_create_user(models.Principal.unverified("bob"))

    assert first.scopes == [models.Scope.ALL]
    assert second.scopes == []


def test_get_or_create_user_is_idempotent(store: SQLLiteStore) -> None:
    first = store.get_or_create_user(models.Principal.unverified("alice"))
    again = store.get_or_create_user(models.Principal.unverified("alice"))

    assert again == first
    assert _count(store, models.User) == 1


def test_get_or_create_project_creates_on_first_call_and_reuses_after(store: SQLLiteStore) -> None:
    first = store.get_or_create_project("p", description="d")
    again = store.get_or_create_project("p", description="ignored on reuse")

    assert again.id == first.id
    assert again.description == "d"
    assert _count(store, models.Project) == 1


def test_get_or_create_project_stamps_created_by(store: SQLLiteStore) -> None:
    actor = store.get_or_create_user(models.Principal.unverified("alice"))

    project = store.get_or_create_project("p", created_by=actor.id)

    assert project.created_by == actor.id


def test_get_or_create_experiment_creates_on_first_call_and_reuses_after(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    first = store.get_or_create_experiment(project.id)
    again = store.get_or_create_experiment(project.id)

    assert again.id == first.id
    assert again.name == "default"
    assert _count(store, models.Experiment) == 1


def test_get_or_create_experiment_tags_source_only_on_first_call(store: SQLLiteStore) -> None:
    project = store.create_project(models.NewProject(name="p", description="d"))

    first = store.get_or_create_experiment(project.id, source=models.ExperimentSource.PYTORCH_LIGHTNING)
    again = store.get_or_create_experiment(project.id, source=None)

    assert first.source == models.ExperimentSource.PYTORCH_LIGHTNING
    assert again.id == first.id
    assert again.source == models.ExperimentSource.PYTORCH_LIGHTNING


def test_get_or_create_experiment_is_scoped_to_its_project(store: SQLLiteStore) -> None:
    project_a = store.create_project(models.NewProject(name="a", description="d"))
    project_b = store.create_project(models.NewProject(name="b", description="d"))

    exp_a = store.get_or_create_experiment(project_a.id, name="default")
    exp_b = store.get_or_create_experiment(project_b.id, name="default")

    assert exp_a.id != exp_b.id


def test_update_user_persists_scope_changes(store: SQLLiteStore) -> None:
    user = store.get_or_create_user(models.Principal.unverified("alice"))
    assert user.scopes == [models.Scope.ALL]  # first user ever, bootstrap admin

    updated = store.update_user(user.model_copy(update={"scopes": [models.Scope.PURGE]}))

    assert updated.scopes == [models.Scope.PURGE]
    assert store.get_or_create_user(models.Principal.unverified("alice")).scopes == [models.Scope.PURGE]


def test_get_or_create_page_is_idempotent_and_updatable(store: SQLLiteStore, experiment_id: int) -> None:
    page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    again = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert again.id == page.id

    updated = page.model_copy(
        update={
            "panels": [PanelInstance(name="metrics")],
            "page_settings": {"open_panel": ["metrics"]},
        }
    )
    stored = store.update_page(updated)

    reloaded = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    assert reloaded.id == page.id
    assert [p.name for p in reloaded.panels] == ["metrics"]
    assert reloaded.page_settings == {"open_panel": ["metrics"]}
    assert stored.id == page.id


def _shared_page_count(store: SQLLiteStore, experiment_id: int) -> int:
    pages = store.tables[models.Page]
    return _count(store, models.Page, pages.c.experiment_id == experiment_id, pages.c.owner_id.is_(None))


def test_get_or_create_page_rejects_a_second_shared_page_for_one_experiment(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """
    `get_or_create_page`'s check-then-insert used to race: two callers that both find no shared
    page yet (e.g. a page load and a script reaching the same brand-new experiment at once) could
    each insert one, leaving two "the" shared pages for one experiment -- reached at random
    depending on which query happened to run first. The partial unique index this now relies on is
    what actually prevents that; proven directly here, bypassing `get_or_create_page` itself to
    insert the second one exactly as a genuinely racing caller would, since reproducing the
    original interleaving deterministically would need real concurrent threads.
    """
    first = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    with pytest.raises(IntegrityError):
        store._execute(
            sa.insert(store.tables[models.Page]).values(
                experiment_id=experiment_id, panels=[], page_settings={}
            )
        )

    assert _shared_page_count(store, experiment_id) == 1
    assert store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id).id == first.id


def test_concurrent_get_or_create_page_converges_on_one_shared_page(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """Same race as above, but the real thing: many threads racing a brand-new experiment at once."""
    page_ids: list[int] = []
    lock = threading.Lock()

    def _call() -> None:
        page = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
        with lock:
            page_ids.append(page.id)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(page_ids) == 8
    assert len(set(page_ids)) == 1
    assert _shared_page_count(store, experiment_id) == 1


def test_experiment_revision_starts_at_zero(store: SQLLiteStore, experiment_id: int) -> None:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    assert experiment.revision == 0


def _mutate_via_create_run(store: SQLLiteStore, experiment_id: int, _run_id: int) -> None:
    store.create_run(models.NewRun(experiment_id=experiment_id))


def _mutate_via_log_metrics(store: SQLLiteStore, experiment_id: int, run_id: int) -> None:
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.1}, step=0, experiment_id=experiment_id, run_id=run_id, timestamp_utc=_TS
            )
        ]
    )


def _mutate_via_log_hyperparams(store: SQLLiteStore, experiment_id: int, run_id: int) -> None:
    store.log_hyperparams(models.NewHyperParams.from_raw(run_id, experiment_id, {"lr": 0.1}))


def _mutate_via_log_artifact_refs(store: SQLLiteStore, experiment_id: int, run_id: int) -> None:
    store.log_artifact_refs(
        [
            models.Artifact(
                key="img", fname="img.png", run_id=run_id, experiment_id=experiment_id, step=0, ref="ref://a"
            )
        ]
    )


@pytest.mark.parametrize(
    # No explicit `ids=` -- pytest already derives a readable id from each function's own
    # `__name__`, which keeps the label and the callable it names impossible to drift apart (a
    # hand-maintained parallel `ids=[...]` list is one to add/reorder without the other).
    "mutate",
    [
        _mutate_via_create_run,
        _mutate_via_log_metrics,
        _mutate_via_log_hyperparams,
        _mutate_via_log_artifact_refs,
    ],
)
def test_mutation_bumps_experiment_revision_by_one(
    store: SQLLiteStore, experiment_id: int, mutate: Callable[[SQLLiteStore, int, int], None]
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    before = store.get_experiment(experiment_id)
    assert before is not None

    mutate(store, experiment_id, run.id)

    after = store.get_experiment(experiment_id)
    assert after is not None
    assert after.revision == before.revision + 1


def test_mutation_does_not_bump_an_unrelated_experiments_revision(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project = store.create_project(models.NewProject(name="other", description="d"))
    other_experiment = store.create_experiment(models.NewExperiment(project_id=project.id))

    store.create_run(models.NewRun(experiment_id=experiment_id))

    assert store.get_experiment(other_experiment.id) == other_experiment


def test_update_experiment_does_not_clobber_a_concurrently_bumped_revision(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """
    `update_experiment` (editing the description) reads an `Experiment` snapshot, edits it, and
    writes it back -- if a metric/run/hyperparam/artifact write bumps `revision` in between, that
    snapshot's own (now-stale) `revision` value must not overwrite the bump.
    """
    stale = store.get_experiment(experiment_id)
    assert stale is not None

    store.create_run(models.NewRun(experiment_id=experiment_id))  # bumps revision to 1, concurrently

    store.update_experiment(stale.model_copy(update={"description": "edited"}))

    after = store.get_experiment(experiment_id)
    assert after is not None
    assert after.description == "edited"
    assert after.revision == 1
