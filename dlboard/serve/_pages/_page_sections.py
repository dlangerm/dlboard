"""
What a plugin adds to a project's or an experiment's page: a line under its header, or a section under its experiments.

Pages are not plugin surface (they are dlboard's own, `serve/_pages/`), with this one exception: a plugin that keeps a
setting of its own (`register_setting`) needs somewhere to show and change it, and the page of what it is set for is where
people look. A section renders in the page's own `layout()`, so it costs no callback round trip to draw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dlboard._compat import StrEnum
from dlboard.models import Experiment, Project
from dlboard.serve._backend._app_slot import AppSlot, current_dash_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from dash.development.base_component import Component


class ProjectSlot(StrEnum):
    """Where on a project's page a section goes."""

    HEADER = "header"
    """A line under the project's name and description: what it is held to, say."""
    BODY = "body"
    """A section under its experiments, above its comparisons."""


class ExperimentSlot(StrEnum):
    """Where on an experiment's page a section goes."""

    HEADER = "header"
    """A line under the experiment's name and description."""


@dataclass(frozen=True)
class PageSection[E, S]:
    """A plugin's part of a page: `render` draws it for the page's entity (or nothing, `None`) at `slot`."""

    slot: S
    render: Callable[[E], Component | None]


class ProjectSection(PageSection[Project, ProjectSlot]):
    """A plugin's part of a project's page."""


class ExperimentSection(PageSection[Experiment, ExperimentSlot]):
    """A plugin's part of an experiment's page."""


class _Sections[E, S]:
    """The sections plugins registered with an app for one kind of page."""

    def __init__(self, name: str) -> None:
        self._slot: AppSlot[list[PageSection[E, S]]] = AppSlot(name)

    def register(self, app: Dash, section: PageSection[E, S]) -> None:
        """Add `section` to every such page, after the ones registered before it."""
        self._slot.setdefault(app, list).append(section)

    def render(self, slot: S, entity: E) -> list[Component]:
        """The sections plugins add to `entity`'s page at `slot`, in the order they registered them."""
        return [
            rendered
            for section in self._slot.find(current_dash_app()) or []
            if section.slot is slot and (rendered := section.render(entity)) is not None
        ]


_PROJECT_SECTIONS = _Sections[Project, ProjectSlot]("project sections")
_EXPERIMENT_SECTIONS = _Sections[Experiment, ExperimentSlot]("experiment sections")

register_project_section = _PROJECT_SECTIONS.register
project_sections = _PROJECT_SECTIONS.render
register_experiment_section = _EXPERIMENT_SECTIONS.register
experiment_sections = _EXPERIMENT_SECTIONS.render
