"""An experiment within a project."""

import pendulum
from pydantic import AwareDatetime, BaseModel, Field

from dlboard._compat import StrEnum


class ExperimentSource(StrEnum):
    """The client integration an experiment's metrics were logged through, when known."""

    PYTORCH_LIGHTNING = "pytorch_lightning"
    """Logged via `dlboard.client.dlboard_logger.DLBoardLogger`, a `pytorch_lightning` `Logger`."""


class NewExperiment(BaseModel, frozen=True, extra="ignore"):
    """An experiment within a project. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    project_id: int
    """The project ID to use for this experiment."""

    name: str = ""
    """The name of the experiment."""

    description: str = ""
    """A description of the experiment."""

    source: ExperimentSource | None = None
    """The client integration this experiment's metrics were logged through, if known -- lets
    server-side views (e.g. chart auto-population) apply integration-specific conventions, such as
    `pytorch_lightning`'s `_step`/`_epoch` metric-name suffixes."""

    created_by: int | None = None
    """The user who created this experiment, if known."""

    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))
    """When this experiment was created."""


class Experiment(NewExperiment, frozen=True, extra="ignore"):
    """An experiment stored in the database. `extra="ignore"`: this crosses the wire -- see `dlboard._wire`."""

    id: int
    """The ID of the experiment."""

    revision: int = 0
    """
    Monotonic counter, bumped by every write that changes what the experiment page renders (new
    run, metric, hyperparameter, or artifact). Server-managed -- never set by a client. A live-update
    poll (see `serve/_pages/_experiment/__init__.py`) detects "something changed" with one cheap
    indexed read instead of re-fetching or diffing the experiment's full contents. A `DataStore`
    implementation not built on `SQLStoreBase` must bump this itself on any experiment-scoped
    mutation to support live updates.
    """

    notes_revision: int = 0
    """
    Like `revision`, but bumped only when a note is posted or deleted -- so the live-update poll can
    refresh an open notes thread without the notes making it re-fetch any chart data.
    """

    last_activity_at: AwareDatetime | None = None
    """
    Server time of the most recent write that bumped `revision`, or `None` if there hasn't been one.

    Server-managed, like `revision`, and stamped by the server's own clock rather than taken from
    logged metrics' client-supplied timestamps -- so "active 2m ago" means data actually arrived 2m
    ago, even from a client replaying an old run.
    """

    deleted_by: int | None = None
    """The user who soft-deleted this experiment, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this experiment was soft-deleted, if at all. See `Project.deleted_at`."""
