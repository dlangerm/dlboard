"""
The read half of the REST API: what was logged, read back a page at a time (#701).

Nothing here reads or answers unbounded data. Every route answers one `Page` of at most its own cap, however many its caller
asks for, and the cursor of the page after it. A page is sought from that cursor: a run's `(created_at, id)`, a value's
`(step, id)`, a key. It is never an offset counted to, so the thousandth page of a long series costs what the first
does, and a run still logging neither skips nor repeats a value. The cursor is opaque to the client: this module alone
reads what it holds. Every read goes through `get_data_store()`, so an API token reads what its user may see and no more.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Final

from flask import request
from pydantic import AwareDatetime, Base64UrlBytes, BaseModel, PositiveInt
from werkzeug.exceptions import BadRequest, NotFound

from dlboard import models
from dlboard._wire import (
    ArtifactInfo,
    LatestValue,
    MetricPoint,
    Page,
    Read,
    Resource,
    create_path,
    read_path,
)
from dlboard.serve import get_data_store

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

MAX_PROJECTS: Final = 100
MAX_EXPERIMENTS: Final = 100
MAX_RUNS: Final = 200
MAX_KEYS: Final = 1000
MAX_VALUES: Final = 5000
"""The most of a series' values a page holds: a long run's loss is a few pages, each one seek."""
MAX_ARTIFACTS: Final = 200


class _Query(BaseModel, frozen=True, extra="ignore"):
    """What a read is asked, as its query string says: how many, from where, and what to keep."""

    limit: PositiveInt | None = None
    cursor: Base64UrlBytes | None = None
    search: str = ""
    key: str | None = None
    tags: frozenset[models.TagName] = frozenset()

    @classmethod
    def of_request(cls) -> _Query:
        return cls.model_validate({**request.args.to_dict(), "tags": request.args.getlist("tag")})

    def size(self, cap: int) -> int:
        """How many the page holds: what was asked for, never more than `cap`."""
        return min(self.limit or cap, cap)

    def after[A: BaseModel](self, shape: type[A]) -> A | None:
        """Where the page starts: the cursor's last row of the page before, or `None` for the first page."""
        return None if self.cursor is None else shape.model_validate_json(self.cursor)

    def series_key(self) -> str:
        if self.key is None:
            msg = "a series is read by its key: ?key=..."
            raise BadRequest(msg)
        return self.key


class _IdAfter(BaseModel, frozen=True, extra="forbid"):
    id: int


class _OffsetAfter(BaseModel, frozen=True, extra="forbid"):
    offset: int


class _RunAfter(BaseModel, frozen=True, extra="forbid"):
    created_at: AwareDatetime
    id: int


class _KeyAfter(BaseModel, frozen=True, extra="forbid"):
    key: str


class _ValueAfter(BaseModel, frozen=True, extra="forbid"):
    step: int
    id: int


class _ArtifactAfter(BaseModel, frozen=True, extra="forbid"):
    step: int | None
    """`None` for one logged at no step, which come after all of those logged at one."""
    id: int


def _paged[T](
    rows: Sequence[T], size: int, cursor_of: Callable[[T], BaseModel]
) -> tuple[Sequence[T], str | None]:
    """`rows`, read one past the page to say whether there is another, as the page and the cursor naming its last row."""
    page = rows[:size]
    if len(rows) <= size:
        return page, None
    return page, base64.urlsafe_b64encode(cursor_of(page[-1]).model_dump_json().encode()).decode()


def _answer(items: Sequence[Any], next_cursor: str | None) -> dict[str, Any]:
    return Page[Any](items=list(items), next_cursor=next_cursor).model_dump(mode="json")


def _run(run_id: int) -> models.Run:
    """The run a read of it names: 404 for one that is gone, or never was, or is not the caller's to see."""
    run = get_data_store().get_run(run_id)
    if run is None:
        raise NotFound
    return run


def _key_page(keys: Sequence[str], query: _Query) -> tuple[Sequence[str], str | None]:
    after = query.after(_KeyAfter)
    size = query.size(MAX_KEYS)
    rest = [key for key in sorted(keys) if after is None or key > after.key][: size + 1]
    return _paged(rest, size, lambda key: _KeyAfter(key=key))


def projects() -> dict[str, Any]:
    """The projects the caller can see, in the order they were made; `search` keeps those whose name has it."""
    query = _Query.of_request()
    after, size, needle = query.after(_IdAfter), query.size(MAX_PROJECTS), query.search.casefold()
    found = sorted(
        (
            project
            for project in get_data_store().get_projects()
            if (after is None or project.id > after.id) and needle in project.name.casefold()
        ),
        key=lambda project: project.id,
    )
    return _answer(*_paged(found[: size + 1], size, lambda project: _IdAfter(id=project.id)))


def project_experiments(entity_id: int) -> dict[str, Any]:
    """A project's experiments, newest first: a page of `search_experiments`, which pages by its own offset."""
    query = _Query.of_request()
    after, size = query.after(_OffsetAfter), query.size(MAX_EXPERIMENTS)
    offset = 0 if after is None else after.offset
    found = get_data_store().search_experiments(
        entity_id, name_contains=query.search, limit=size + 1, offset=offset
    )
    return _answer(*_paged(found.experiments, size, lambda _: _OffsetAfter(offset=offset + size)))


def experiment_runs(entity_id: int) -> dict[str, Any]:
    """An experiment's runs, newest first, sought from the `(created_at, id)` of the last run of the page before."""
    query = _Query.of_request()
    after, size = query.after(_RunAfter), query.size(MAX_RUNS)
    runs = get_data_store().get_runs(
        entity_id,
        limit=size + 1,
        run_ids=None,
        name_contains=query.search,
        tags=query.tags,
        before=None if after is None else (after.created_at, after.id),
    )
    return _answer(*_paged(list(runs), size, lambda run: _RunAfter(created_at=run.created_at, id=run.id)))


def experiment_metric_keys(entity_id: int) -> dict[str, Any]:
    """The keys an experiment's runs logged metrics under, a page of them: found by hopping the index from key to key."""
    return _answer(*_key_page(get_data_store().summarize_metric_keys(entity_id), _Query.of_request()))


def experiment_artifact_keys(entity_id: int) -> dict[str, Any]:
    """The keys an experiment's runs logged artifacts under, a page of them."""
    keys = [artifact.key for artifact in get_data_store().summarize_artifact_keys(entity_id)]
    return _answer(*_key_page(keys, _Query.of_request()))


def run_hyperparams(entity_id: int) -> dict[str, Any]:
    """A run's hyperparameters, as it logged them: one object, empty if it logged none."""
    run = _run(entity_id)
    logged = get_data_store().fetch_hyperparams(run.experiment_id, run_ids=frozenset({run.id}))
    return next((hyperparams.hparams_dict for hyperparams in logged), {})


def run_summary(entity_id: int) -> dict[str, Any]:
    """A run's latest value under each of a page of its experiment's keys: a probe per key, never its rows."""
    run, store, query = _run(entity_id), get_data_store(), _Query.of_request()
    keys, next_cursor = _key_page(store.summarize_metric_keys(run.experiment_id), query)
    latest = (
        store.fetch_metrics(
            run.experiment_id,
            keys=frozenset(keys),
            run_ids=frozenset({run.id}),
            reduction=models.MetricReduction.LATEST,
        )
        .latest_per_run()
        .get(run.id, {})
    )
    return _answer([LatestValue(key=key, value=latest[key]) for key in keys if key in latest], next_cursor)


def run_series(entity_id: int) -> dict[str, Any]:
    """Every value a run logged under `key`, by step: a page sought from the `(step, id)` of the last value before it."""
    run, query = _run(entity_id), _Query.of_request()
    after, size = query.after(_ValueAfter), query.size(MAX_VALUES)
    rows = get_data_store().metric_series(
        run.experiment_id,
        run_id=run.id,
        key=query.series_key(),
        after=None if after is None else (after.step, after.id),
        limit=size + 1,
    )
    page, next_cursor = _paged(rows, size, lambda row: _ValueAfter(step=row.step, id=row.id))
    points = [MetricPoint(step=row.step, value=row.value, timestamp_utc=row.timestamp_utc) for row in page]
    return _answer(points, next_cursor)


def run_artifacts(entity_id: int) -> dict[str, Any]:
    """A run's artifacts under `key`, without their bytes: those logged at a step by step, then those logged at none."""
    run, query = _run(entity_id), _Query.of_request()
    after, size = query.after(_ArtifactAfter), query.size(MAX_ARTIFACTS)
    rows = get_data_store().artifact_series(
        run.experiment_id,
        run_id=run.id,
        key=query.series_key(),
        after=None if after is None else (after.step, after.id),
        limit=size + 1,
    )
    page, next_cursor = _paged(rows, size, lambda row: _ArtifactAfter(step=row.step, id=row.id or 0))
    return _answer([ArtifactInfo.model_validate(row, from_attributes=True) for row in page], next_cursor)


READ_ROUTES: Final[tuple[tuple[str, list[str], Callable[..., Any]], ...]] = (
    (create_path(Resource.PROJECTS), ["GET"], projects),
    (read_path(Resource.PROJECTS, Read.EXPERIMENTS), ["GET"], project_experiments),
    (read_path(Resource.EXPERIMENTS, Read.RUNS), ["GET"], experiment_runs),
    (read_path(Resource.EXPERIMENTS, Read.METRIC_KEYS), ["GET"], experiment_metric_keys),
    (read_path(Resource.EXPERIMENTS, Read.ARTIFACT_KEYS), ["GET"], experiment_artifact_keys),
    (read_path(Resource.RUNS, Read.HYPERPARAMS), ["GET"], run_hyperparams),
    (read_path(Resource.RUNS, Read.SUMMARY), ["GET"], run_summary),
    (read_path(Resource.RUNS, Read.SERIES), ["GET"], run_series),
    (read_path(Resource.RUNS, Read.ARTIFACTS), ["GET"], run_artifacts),
)
