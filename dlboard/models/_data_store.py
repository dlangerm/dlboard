"""Defines the protocol for interacting with an arbitrary data store."""

from __future__ import annotations

import dataclasses
import typing

from dlboard.models._metric import MetricReduction, SeriesBand
from dlboard.models._pin import PinColor

SUMMARY_RUNS: typing.Final = 100
"""
How many of an experiment's newest runs a summary judges by: the key summaries and the column catalog read no more than
these, so what they cost does not grow with how many runs an experiment has.
"""


@dataclasses.dataclass(frozen=True)
class StepRange:
    """The steps from `first` to `last`, both included."""

    first: int
    last: int


# Pre-3.12 `TypeVar`/`ParamSpec` style (the client's floor is 3.10) -- `P` is `get_or_create`'s
# constructor signature; `Dataframe`/`Chart` are `Page`'s own two type parameters,
# reused here rather than declared fresh per method (PEP 695's per-method `def foo[T](...)` needs
# 3.12).
_P = typing.ParamSpec("_P")
_Dataframe = typing.TypeVar("_Dataframe")
_Chart = typing.TypeVar("_Chart")

if typing.TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping
    from datetime import datetime

    from flask import Response
    from pydantic import AnyUrl
    from typing_extensions import Self
    from werkzeug.datastructures import FileStorage

    from dlboard import models
    from dlboard.serve._backend._metric_frame import MetricFrame, MetricKeySummary


class DataStore(typing.Protocol[_P]):
    """Any data store."""

    @classmethod
    def get_or_create(cls, *args: _P.args, **kwargs: _P.kwargs) -> Self:
        """Initialize a data store."""
        ...

    def get_or_create_user(self, principal: models.Principal) -> models.User:
        """
        The user for `principal`'s `(issuer, subject)`, creating it the first time it's seen.

        Refreshes a known user's `email`/`groups` from `principal`. Brand new users are granted no
        scopes -- except the very first `UNVERIFIED_ISSUER` user a store ever creates, which gets
        `Scope.ALL` so a local single-user install has a working admin with zero configuration.
        Raises `ValueError` if `principal.username` is already taken by a different identity.
        """
        ...

    def get_user(self, user_id: int) -> models.User | None:
        """A user by id."""
        ...

    def find_user(self, username: str) -> models.User | None:
        """A user by their (unique) username."""
        ...

    def update_user(self, user: models.User) -> models.User:
        """Update a user, e.g. to grant/revoke scopes or disable them. For admin-driven user management."""
        ...

    def list_project_grants(self, project_ids: Collection[int]) -> list[models.ProjectGrant]:
        """Every grant on any of `project_ids`."""
        ...

    def grants_for(self, user: models.User) -> list[models.ProjectGrant]:
        """Every grant naming `user` directly, or one of `user.groups`."""
        ...

    def set_project_grant(self, grant: models.NewProjectGrant) -> models.ProjectGrant:
        """Grant a role on a project, replacing whatever role that same user/group already had there."""
        ...

    def delete_project_grant(self, project_id: int, grant_id: int) -> None:
        """Remove one of `project_id`'s grants."""
        ...

    def create_api_token(self, token: models.NewApiToken) -> models.ApiToken:
        """Store a freshly minted API token (its hash -- see `NewApiToken.secret_hash`)."""
        ...

    def get_api_token(self, token_id: str) -> models.ApiToken | None:
        """An API token by its public `token_id`, revoked or not."""
        ...

    def list_api_tokens(self, user_id: int) -> list[models.ApiToken]:
        """`user_id`'s API tokens, newest first, including revoked ones."""
        ...

    def revoke_api_token(self, api_token_id: int, user_id: int) -> None:
        """Revoke one of `user_id`'s API tokens; anyone else's is left alone."""
        ...

    def touch_api_token(self, api_token_id: int) -> None:
        """Record that an API token was just used."""
        ...

    def get_password_credential(self, user_id: int) -> models.PasswordCredential | None:
        """`user_id`'s password hash, if they have one."""
        ...

    def set_password_credential(self, credential: models.PasswordCredential) -> None:
        """Set (or replace) a user's password hash."""
        ...

    def create_project(self, project: models.NewProject) -> models.Project:
        """Create a project."""
        ...

    def get_or_create_project(
        self, name: str, description: str = "", created_by: int | None = None
    ) -> models.Project:
        """Get the project named `name`, creating it (with `description`) if it doesn't exist yet."""
        ...

    def get_project(self, database_id: int) -> models.Project | None:
        """A (non-deleted) project by id."""
        ...

    def get_projects(self) -> typing.Iterator[models.Project]:
        """Get all projects."""
        ...

    def get_project_stats(self) -> dict[int, models.ProjectStats]:
        """Every (non-deleted) project's activity, keyed by project id -- in one read, for a listing."""
        ...

    def update_project(self, project: models.Project) -> models.Project:
        """Update a project."""
        ...

    def create_experiment(self, experiment: models.NewExperiment) -> models.Experiment:
        """Create an experiment within a project."""
        ...

    def get_or_create_experiment(
        self,
        project_id: int,
        name: str = "default",
        created_by: int | None = None,
        source: models.ExperimentSource | None = None,
    ) -> models.Experiment:
        """
        Get the named experiment within a project, creating it if it doesn't exist yet.

        `source` only applies the first time `name` is seen within `project_id` -- once the
        experiment exists, later calls just reuse it as-is.
        """
        ...

    def get_experiment(self, database_id: int) -> models.Experiment | None:
        """Get an experiment by id."""
        ...

    def get_experiments(self, project_id: int) -> typing.Iterator[models.Experiment]:
        """Get all experiments for a project."""
        ...

    def get_experiment_stats(self, project_id: int) -> dict[int, models.ActivityStats]:
        """Each of a project's (non-deleted) experiments' activity, keyed by experiment id -- in one read."""
        ...

    def update_experiment(self, experiment: models.Experiment) -> models.Experiment:
        """Update an experiment."""
        ...

    def create_run(self, run: models.NewRun) -> models.Run:
        """Create a new run for an experiment."""
        ...

    def get_run(self, run_id: int) -> models.Run | None:
        """A (non-deleted) run by id."""
        ...

    def finish_run(self, run_id: int, status: models.RunStatus, ended_at: datetime) -> models.Run:
        """
        Record that a run is done: its `status` and when it `ended_at`. Raises if it is missing or deleted.

        Safe to call again (a later stage reporting done): the newest report wins.
        """
        ...

    def get_runs(
        self,
        experiment_id: int,
        *,
        limit: int | None = 1000,
        offset: int = 0,
        run_ids: frozenset[int] | None,
        name_contains: str = "",
    ) -> typing.Iterator[models.Run]:
        """
        A page of an experiment's (non-deleted) runs, most recently created first.

        A `limit` of `None` is every run, and `run_ids` limits it to those, as it does every read of what runs logged:
        `None` is all of the experiment's, an empty set none. `name_contains` keeps the runs whose name has it in it,
        whatever its case, or whose id it is: what a picker of runs searches by.
        """
        ...

    def log_metrics(self, metric: Iterable[models.LoggedMetrics]) -> None:
        """Log metrics to the data store."""
        ...

    def fetch_metrics(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        run_ids: frozenset[int] | None,
        reduction: MetricReduction = MetricReduction.HISTORY,
    ) -> MetricFrame:
        """
        An experiment's (non-deleted runs') metrics -- only `keys`, if given, else every metric.

        Every step, unless a `reduction` asks for less (`MetricReduction`): then a row per run and key, found
        without reading the rest, so what it costs does not grow with how long the runs trained.

        A `reduction` is a hint, not a contract: a store may always return every step instead, and the reader
        reduces it itself (a chart's own reduction of the rows it is handed gives the same answer). So a store that
        cannot reduce cheaply need not, and a `MetricReduction` added later does not break one that predates it.
        """

    def summarize_metric_keys(self, experiment_id: int) -> list[str]:
        """
        Every metric key logged in an experiment (non-deleted runs), sorted, without fetching values.

        For callers (a column picker, chart suggestions) that only need to know what's *available*
        -- fetching every metric row via `fetch_metrics` for that is needlessly expensive once an
        experiment has any real volume of logged steps. Judging a key's shape is a probe per key, so that is
        `summarize_metric_steps`, for the keys a caller is about to chart and no others.
        """
        ...

    def summarize_metric_steps(self, experiment_id: int, keys: frozenset[str]) -> list[MetricKeySummary]:
        """
        How many steps each of `keys` logged in a run, at most (see `MetricKeySummary`), judged on the newest runs.

        A key no live run logged comes back with 0. Probes only `keys`, so it costs what asking for them costs.
        """
        ...

    def fetch_series_band(
        self,
        experiment_id: int,
        *,
        groups: Mapping[str, str],
        run_ids: frozenset[int] | None,
        grid: int,
    ) -> list[SeriesBand]:
        """
        Per live run and group, the spread of its keys' values over the cells of a grid of at most `grid` steps.

        `groups` maps each key to the label it is grouped under, so one read gives a band for each class of a family
        of keys (`""` throughout for a single band). The grid runs from the first step any key logged to the last,
        which ends the last cell, and is as fine as `grid` allows. A key counts in a cell if its newest write there is
        a number, however its steps are spaced (a run logged every 50 steps still has a value in each cell that
        holds one of them), and a cell nothing was logged in has no band. What is read is what the grid needs, not
        every step a run trained.
        """
        ...

    def fetch_histograms(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str],
        max_steps: int,
        run_ids: frozenset[int] | None,
    ) -> list[models.UnderlyingHistogramTableEntry]:
        """
        An experiment's (non-deleted runs') histograms under `keys`, thinned to about `max_steps` steps per run and key.

        Histograms are logged through `log_metrics`. Thinning happens in the store, before anything is read
        back, so what a chart costs does not grow with how long a run has trained.
        """
        ...

    def summarize_histogram_keys(self, experiment_id: int) -> list[str]:
        """Every histogram key logged in an experiment (non-deleted runs), sorted, without fetching any."""
        ...

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> models.HyperParams:
        """Log hyperparameters to the data store."""
        ...

    def fetch_hyperparams(
        self, experiment_id: int, *, run_ids: frozenset[int] | None
    ) -> typing.Iterator[models.HyperParams]:
        """Every (non-deleted) run's hyperparameters for an experiment."""
        ...

    def get_or_create_page(
        self,
        page_type: type[models.Page[_Dataframe, _Chart]],
        *,
        run_id: int | None = None,
        experiment_id: int | None = None,
        project_id: int | None = None,
        new_page_type: type[models.NewPage[_Dataframe, _Chart]] | None = None,
    ) -> models.Page[_Dataframe, _Chart]:
        """Fetch the pages for a run, experiment, or project."""
        ...

    def update_page(self, page: models.Page[_Dataframe, _Chart]) -> models.Page[_Dataframe, _Chart]:
        """Update a page."""
        ...

    def create_view(
        self,
        page_type: type[models.Page[_Dataframe, _Chart]],
        view: models.NewPage[_Dataframe, _Chart],
    ) -> models.Page[_Dataframe, _Chart]:
        """Save a named, owned view of a page (see `NewPage.owner_id`) as its own page."""
        ...

    def get_view(
        self, page_type: type[models.Page[_Dataframe, _Chart]], view_id: int
    ) -> models.Page[_Dataframe, _Chart] | None:
        """A named view by id, or `None` if there's no such view."""
        ...

    def list_views(self, experiment_id: int, viewer_id: int) -> list[models.ViewSummary]:
        """The views of an experiment's page visible to `viewer_id`: their own, plus anyone's shared ones."""
        ...

    def delete_view(self, view_id: int, owner_id: int) -> None:
        """Delete one of `owner_id`'s views; never anyone else's, nor a shared page."""
        ...

    def add_comment(self, comment: models.NewComment) -> models.Comment:
        """Post a note to an experiment's thread, bumping its `notes_revision`."""
        ...

    def list_comments(self, experiment_id: int) -> list[models.Comment]:
        """An experiment's notes, oldest first."""
        ...

    def delete_comment(self, comment_id: int, author_id: int) -> None:
        """Delete one of `author_id`'s own notes (bumping `notes_revision`); never anyone else's."""
        ...

    def pin_runs(
        self,
        pinned_by: int,
        run_ids: frozenset[int],
        color: PinColor = PinColor.PLAIN,
    ) -> None:
        """
        Pin `run_ids` for `pinned_by` in `color`.

        A run `pinned_by` already pinned in that colour keeps its pin. Raises for a run that does not exist or has been deleted.
        """
        ...

    def unpin_runs(self, pinned_by: int, run_ids: frozenset[int], color: PinColor) -> None:
        """Remove `pinned_by`'s pin of `color` from each of `run_ids`; never anyone else's, nor another colour's."""
        ...

    def run_experiments(self, run_ids: frozenset[int]) -> dict[int, int]:
        """The experiment each of `run_ids` is of, whether or not it is deleted; a run that does not exist is left out."""
        ...

    def pins_of(
        self, experiment_id: int, *, run_ids: frozenset[int] | None
    ) -> dict[int, list[models.NamedPin]]:
        """The pins on an experiment's (non-deleted) runs, by run, with who pinned each: only `run_ids`' if given, `None` being every run's."""
        ...

    def list_pinned_runs(  # noqa: PLR0913 -- one keyword per filter of the pinned-runs list
        self,
        project_id: int,
        *,
        colors: frozenset[PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> models.PinnedRuns:
        """
        A page of a project's pinned (non-deleted) runs, the one pinned last first, and how many runs matched in all.

        A run is listed once however many pins it has, with every one of them. It matches if any of its pins is of
        `colors`, by `pinned_by`, in `experiment_id`, and the run's name (or id) has `name_contains` in it, whatever its
        case, if each is given; `None` for any of them is no filter.
        """
        ...

    def pin_counts(self, project_id: int) -> dict[PinColor, int]:
        """How many of a project's (non-deleted) runs are pinned in each colour by anyone; a colour nobody used is left out."""
        ...

    def pinned_run_ids(  # noqa: PLR0913 -- the filters of `list_pinned_runs`, and how many runs
        self,
        project_id: int,
        *,
        colors: frozenset[PinColor] | None = None,
        pinned_by: frozenset[int] | None = None,
        experiment_id: int | None = None,
        name_contains: str = "",
        limit: int,
    ) -> list[int]:
        """
        The runs `list_pinned_runs` would list for these filters, the one pinned last first; at most `limit`.

        The ids alone, for what acts on the runs (opening them as a comparison) and does not draw them.
        """
        ...

    def list_pinners(self, project_id: int) -> list[models.Pinner]:
        """Who has pinned a run of a project (not a deleted one), by username: the people a pinned-runs filter can offer."""
        ...

    def list_users(self) -> list[models.User]:
        """Every user, by username."""
        ...

    def log_artifact_refs(self, artifacts: Iterable[models.Artifact]) -> None:
        """Log a set of artifacts."""
        ...

    def count_artifacts_by_ref(self, ref: str) -> int:
        """
        How many artifact rows (deleted or not) already point at `ref`.

        `link_artifacts` is the only way two rows could ever end up sharing one ref -- an ordinary
        upload always writes to a freshly hashed path -- so this is what keeps it from ever
        actually happening: a non-zero count means `ref` already belongs to some other artifact,
        linking it again would let that artifact's owner read (or, on purge, silently orphan) the
        first one's blob.
        """
        ...

    def get_artifact(self, artifact_id: int) -> models.Artifact | None:
        """Get one (non-deleted) artifact by id, or `None` if it doesn't exist (or has been deleted)."""
        ...

    def fetch_artifacts(
        self,
        experiment_id: int,
        *,
        keys: frozenset[str] | None = None,
        key_prefixes: frozenset[str] = frozenset(),
        run_ids: frozenset[int] | None,
        steps: StepRange | None = None,
    ) -> typing.Iterator[models.Artifact]:
        """
        An experiment's (non-deleted) artifact metadata, not bytes.

        Only the artifacts under `keys` and under any key starting with one of `key_prefixes`, if either
        is given -- `keys=None` with no prefixes means every artifact. Only those logged at `steps`, if given.
        """
        ...

    def artifact_steps(
        self, experiment_id: int, *, key: str, run_ids: frozenset[int] | None
    ) -> dict[int, list[int]]:
        """
        The steps each run logged a (non-deleted) artifact under `key` at, in order, by run: none of their metadata.

        What a chart that shows one step at a time (an image chart's slider) can go to, read without the artifacts.
        """
        ...

    def summarize_artifact_keys(self, experiment_id: int) -> list[models.Artifact]:
        """
        An experiment's latest (non-deleted) artifact under each key, sorted by key.

        For callers that need only what's *available* -- which keys, and what kind of thing each holds --
        where `fetch_artifacts` would read an artifact for every step an experiment logged.
        """
        ...

    def delete_project(self, project_id: int, actor: models.User) -> None:
        """Soft-delete a project and cascade to its experiments, runs, and artifacts. Requires `Scope.PROJECT_DELETE`."""
        ...

    def restore_project(self, project_id: int, actor: models.User) -> None:
        """Restore a soft-deleted project and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_project(self, project_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted project. Requires `Scope.PURGE`."""
        ...

    def delete_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Soft-delete an experiment and cascade to its runs and artifacts. Requires `Scope.EXPERIMENT_DELETE`."""
        ...

    def restore_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Restore a soft-deleted experiment and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_experiment(self, experiment_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted experiment. Requires `Scope.PURGE`."""
        ...

    def delete_run(self, run_id: int, actor: models.User) -> None:
        """Soft-delete a run and cascade to its artifacts. Requires `Scope.RUN_DELETE`."""
        ...

    def restore_run(self, run_id: int, actor: models.User) -> None:
        """Restore a soft-deleted run and everything deleted with it. Requires `Scope.RESTORE`."""
        ...

    def purge_run(self, run_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted run. Requires `Scope.PURGE`."""
        ...

    def delete_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Soft-delete a single artifact. Requires `Scope.ARTIFACT_DELETE`."""
        ...

    def restore_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Restore a soft-deleted artifact. Requires `Scope.RESTORE`."""
        ...

    def purge_artifact(self, artifact_id: int, actor: models.User) -> None:
        """Permanently delete an already soft-deleted artifact. Requires `Scope.PURGE`."""
        ...

    def list_deleted_projects(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Project]:
        """List soft-deleted projects, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_experiments(
        self, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.Experiment]:
        """List soft-deleted experiments, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_runs(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Run]:
        """List soft-deleted runs, most recently deleted first, for a trash/admin view."""
        ...

    def list_deleted_artifacts(self, limit: int = 100, offset: int = 0) -> typing.Iterator[models.Artifact]:
        """List soft-deleted artifacts, most recently deleted first, for a trash/admin view."""
        ...

    def list_audit_log(
        self, actor: models.User, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.AuditLogEntry]:
        """List audit log entries, most recent first, for a trash/admin view. Requires `Scope.AUDIT_LOG_READ`."""
        ...

    def list_pending_artifact_purges(
        self, limit: int = 100, offset: int = 0
    ) -> typing.Iterator[models.ArtifactPurgeTask]:
        """List artifact blobs still waiting to be deleted from the `ArtifactStore`, oldest first."""
        ...

    def count_pending_artifact_purges(self) -> int:
        """Count artifact blobs still waiting to be deleted. 0 means the last purge fully cleaned up."""
        ...

    def complete_artifact_purge(self, task_id: int) -> None:
        """Record that a queued blob deletion succeeded by deleting its task row."""
        ...

    def fail_artifact_purge(self, task_id: int, error: str) -> None:
        """Record that a queued blob deletion failed. The task stays pending and is retried later."""
        ...

    def artifact_purge_progress_at(self) -> datetime | None:
        """
        When a running purge last made progress; `None` while none is running.

        Shared by every server process, so any of them can tell a purge in progress from one a process died in.
        """
        ...

    def record_artifact_purge_progress(self, *, running: bool) -> None:
        """Stamp that a purge is making progress now (`running`), or that it is done."""
        ...


class ArtifactStore(typing.Protocol[_P]):
    """An artifact store for files and arbitrary byte-like data."""

    @classmethod
    def get_or_create(cls, *args: _P.args, **kwargs: _P.kwargs) -> Self:
        """Initialize a data store."""
        ...

    def log_artifacts(self, artifacts: Iterable[tuple[models.NewArtifact, FileStorage]]) -> None:
        """Log a set of artifacts, each paired with its own uploaded file."""
        ...

    def link_artifacts(self, links: Iterable[tuple[models.NewArtifact, AnyUrl]]) -> list[models.Artifact]:
        """
        Register a set of artifacts already stored at their given ref, with no bytes moved.

        Raises `UnservableArtifactRefError` for any ref this store won't serve (outside its own space
        and not on an allowlist, or nothing actually stored there yet) -- checked for the whole
        batch before any of it is registered, the
        same all-or-nothing way `DataStore.log_artifact_refs` treats artifacts it must reject outright.
        """
        ...

    def download_artifact(self, ref: AnyUrl) -> Response:
        """Download an artifact given a url."""
        ...

    def delete_artifact(self, ref: AnyUrl) -> None:
        """Permanently delete one artifact blob. Idempotent -- a blob that's already gone is not an error."""
        ...
