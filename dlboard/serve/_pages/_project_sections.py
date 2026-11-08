"""
What a plugin adds to a project's page: a line under its header, or a section under its experiments.

Pages are not plugin surface (they are dlboard's own, `serve/_pages/`), with this one exception: a plugin that keeps a
project's setting of its own (`register_setting`) needs somewhere to show and change it, and the project page is where
its owners look. A section renders in the page's own `layout()`, so it costs no callback round trip to draw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dlboard._compat import StrEnum
from dlboard.serve._backend._app_slot import AppSlot, current_dash_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.models import Project


class ProjectSlot(StrEnum):
    """Where on a project's page a section goes."""

    HEADER = "header"
    """A line under the project's name and description: what it is held to, say."""
    BODY = "body"
    """A section under its experiments, above its comparisons."""


@dataclass(frozen=True)
class ProjectSection:
    """A plugin's part of a project's page: `render` draws it for a project (or nothing, `None`)."""

    slot: ProjectSlot
    render: Callable[[Project], Component | None]


PROJECT_SECTIONS: AppSlot[list[ProjectSection]] = AppSlot("project sections")


def register_project_section(app: Dash, section: ProjectSection) -> None:
    """Add `section` to every project's page, after the ones registered before it."""
    PROJECT_SECTIONS.setdefault(app, list).append(section)


def project_sections(slot: ProjectSlot, project: Project) -> list[Component]:
    """The sections plugins add to `project`'s page at `slot`, in the order they registered them."""
    return [
        rendered
        for section in PROJECT_SECTIONS.find(current_dash_app()) or []
        if section.slot is slot and (rendered := section.render(project)) is not None
    ]
