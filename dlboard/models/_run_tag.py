"""Labels on runs, shared by everyone who can see the project: `baseline`, `ablation/lr`, `paper-fig-3`."""

from __future__ import annotations

import re
from typing import Annotated, Final

import pendulum
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, StringConstraints

MAX_TAG_LENGTH: Final = 64


def normalize_tag(name: str) -> str:
    """A tag's name as stored and compared: whitespace collapsed, lowercased, so `Baseline ` and `baseline` are one tag."""
    return re.sub(r"\s+", " ", name).strip().lower()


TagName = Annotated[
    str, AfterValidator(normalize_tag), StringConstraints(min_length=1, max_length=MAX_TAG_LENGTH)
]
"""A run tag's name, normalized: at most `MAX_TAG_LENGTH` characters, never empty."""


class NewTag(BaseModel, frozen=True, extra="ignore"):
    """A tag of a project's runs, made the first time a run of the project is given it."""

    project_id: int
    name: TagName
    created_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class Tag(NewTag, frozen=True, extra="ignore"):
    """A tag as stored."""

    id: int


class NewRunTag(BaseModel, frozen=True, extra="ignore"):
    """A run given a tag. The run's experiment rides along, as a pin's does, so a tag's runs are found by experiment."""

    run_id: int
    tag_id: int
    experiment_id: int
    tagged_by: int | None = None
    tagged_at: AwareDatetime = Field(default_factory=lambda: pendulum.now(pendulum.UTC))


class RunTag(NewRunTag, frozen=True, extra="ignore"):
    """A run's tag as stored."""

    id: int
