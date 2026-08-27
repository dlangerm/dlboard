"""An experiment within a project."""

from enum import StrEnum

import pendulum
from pydantic import AwareDatetime, BaseModel, Field


class ExperimentSource(StrEnum):
    """The client integration an experiment's metrics were logged through, when known."""

    PYTORCH_LIGHTNING = "pytorch_lightning"
    """Logged via `dltrack.client.dltrack_logger.DLTrackLogger`, a `pytorch_lightning` `Logger`."""


class NewExperiment(BaseModel, frozen=True, extra="forbid"):
    """An experiment within a project."""

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


class Experiment(NewExperiment, frozen=True, extra="forbid"):
    """An experiment stored in the database."""

    id: int
    """The ID of the experiment."""

    deleted_by: int | None = None
    """The user who soft-deleted this experiment, if it's been deleted."""

    deleted_at: AwareDatetime | None = None
    """When this experiment was soft-deleted, if at all. See `Project.deleted_at`."""
