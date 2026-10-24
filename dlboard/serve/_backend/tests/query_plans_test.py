"""Every experiment-scoped read of a table is answered by seeking into an index, not by scanning."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from dlboard import models
from dlboard.conftest import StoreBackend
from dlboard.models import MetricReduction

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from dlboard.plugins.data_stores.postgres import PostgresSettings, PostgresStore

pytestmark = pytest.mark.postgres

_SEEKS = {
    "UnderlyingMetricTableEntry": "(experiment_id, key, run_id, step)",
    "UnderlyingHistogramTableEntry": "(experiment_id, key, run_id, step)",
    "Artifact": "(experiment_id, key, run_id, step)",
    "HyperParams": "(experiment_id, run_id)",
    "Run": "(experiment_id, created_at)",
}
"""What each table an experiment page reads is sought by: the index that leads with the experiment (or the key, `id`)."""
_BY_RUN = frozenset({"UnderlyingMetricTableEntry", "UnderlyingHistogramTableEntry", "Artifact"})
"""The per-step tables, which deleting a run reaches by its `run_id`."""
_READS: dict[str, Callable[[PostgresStore, int], object]] = {
    "summarize_metric_keys": lambda s, e: s.summarize_metric_keys(e),
    "summarize_metric_steps": lambda s, e: s.summarize_metric_steps(e, frozenset({"loss"})),
    "fetch_series_band": lambda s, e: s.fetch_series_band(e, groups={"loss": ""}, run_ids=None, grid=4),
    "summarize_histogram_keys": lambda s, e: s.summarize_histogram_keys(e),
    "summarize_artifact_keys": lambda s, e: s.summarize_artifact_keys(e),
    "fetch_metrics, latest": lambda s, e: s.fetch_metrics(e, reduction=MetricReduction.LATEST, run_ids=None),
    "fetch_metrics, first": lambda s, e: s.fetch_metrics(e, reduction=MetricReduction.FIRST, run_ids=None),
    "fetch_metrics, mean": lambda s, e: s.fetch_metrics(e, reduction=MetricReduction.MEAN, run_ids=None),
    "fetch_metrics": lambda s, e: s.fetch_metrics(e, run_ids=None),
    "fetch_metrics, some keys": lambda s, e: s.fetch_metrics(e, keys=frozenset({"loss"}), run_ids=None),
    "fetch_histograms": lambda s, e: s.fetch_histograms(
        e, keys=frozenset({"grad"}), max_steps=10, run_ids=None
    ),
    "fetch_artifacts": lambda s, e: list(s.fetch_artifacts(e, run_ids=None)),
    "get_runs, the newest": lambda s, e: list(s.get_runs(e, limit=20, run_ids=None)),
    "get_runs, every one": lambda s, e: list(s.get_runs(e, limit=None, run_ids=None)),
    "fetch_hyperparams": lambda s, e: list(s.fetch_hyperparams(e, run_ids=None)),
    "fetch_hyperparams, some runs": lambda s, e: list(s.fetch_hyperparams(e, run_ids=frozenset({1, 2}))),
}


@pytest.fixture
def store_backend() -> StoreBackend:
    return StoreBackend.POSTGRES


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _recorded(store: PostgresStore, action: Callable[[], object]) -> list[tuple[str, dict[str, object]]]:
    """The statements (and their parameters) `action` sent to the database."""
    statements: list[tuple[str, dict[str, object]]] = []

    def record(
        _conn: sa.Connection,
        _cursor: object,
        statement: str,
        parameters: dict[str, object],
        _context: object,
        _many: bool,
    ) -> None:
        statements.append((statement, parameters))

    sa.event.listen(store._engine, "before_cursor_execute", record)
    action()
    sa.event.remove(store._engine, "before_cursor_execute", record)
    return statements


def _plan(
    store: PostgresStore, settings: PostgresSettings, statement: str, parameters: dict[str, object]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The plan nodes of `statement` when scanning a table is forbidden, and the definitions of the schema's indexes."""
    with store._engine.connect() as conn:
        # A table this small is always cheaper to scan, so forbid scans: what is left is whether an index can serve the read.
        conn.exec_driver_sql("SET enable_seqscan = off")
        document: Any = conn.exec_driver_sql("EXPLAIN (FORMAT JSON) " + statement, parameters).scalar_one()
        definitions = conn.execute(
            sa.text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :schema"),
            {"schema": settings.db_schema},
        ).all()
    plan: dict[str, Any] = (json.loads(document) if isinstance(document, str) else document)[0]["Plan"]
    return list(_nodes(plan)), {str(name): str(definition) for name, definition in definitions}


@pytest.mark.parametrize("read", _READS)
def test_a_read_seeks_a_table_by_experiment(
    store: PostgresStore, postgres_settings: PostgresSettings, experiment_id: int, read: str
) -> None:
    statement, parameters = _recorded(store, lambda: _READS[read](store, experiment_id))[-1]
    nodes, indexes = _plan(store, postgres_settings, statement, parameters)

    scanned = {n["Relation Name"] for n in nodes if n["Node Type"] == "Seq Scan"} & _SEEKS.keys()
    assert not scanned
    # Postgres can run a condition on an index's later column too, but it reads the whole index to: the lead column is the seek.
    # Fetching chosen rows by `id` is a seek as well.
    for index in {n["Index Name"] for n in nodes if "Index Name" in n}:
        for table, seek in _SEEKS.items():
            if f'"{table}"' in indexes[index]:
                assert indexes[index].endswith((seek, "(id)")), index


def test_deleting_and_purging_a_run_seek_the_per_step_tables_by_run(
    store: PostgresStore,
    postgres_settings: PostgresSettings,
    experiment_id: int,
    admin: models.User,
) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    def delete_and_purge() -> None:
        store.delete_run(run.id, admin)
        store.purge_run(run.id, admin)

    statements = _recorded(store, delete_and_purge)
    # The database deletes the run's rows itself (`ON DELETE CASCADE`), by a statement of its own that no hook sees.
    cascade: list[tuple[str, dict[str, object]]] = [
        (f'DELETE FROM "{table}" WHERE run_id = %(run_id)s', {"run_id": run.id}) for table in _BY_RUN
    ]

    reached = [
        (statement, parameters)
        for statement, parameters in [*statements, *cascade]
        if any(f'"{table}"' in statement for table in _BY_RUN) and not statement.startswith("INSERT")
    ]
    assert len(reached) > len(_BY_RUN)
    for statement, parameters in reached:
        nodes, indexes = _plan(store, postgres_settings, statement, parameters)
        assert not {n["Relation Name"] for n in nodes if n["Node Type"] == "Seq Scan"} & _BY_RUN, statement
        # With scans forbidden Postgres reads a whole index it can only filter by `run_id`, so the seek is the index's lead.
        for index in {n["Index Name"] for n in nodes if "Index Name" in n}:
            if any(f'"{table}"' in indexes[index] for table in _BY_RUN):
                assert indexes[index].endswith(("(run_id)", "(id)")), (statement, index)
