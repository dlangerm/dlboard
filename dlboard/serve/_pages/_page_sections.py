"""
What a plugin adds to a project's or an experiment's page: a tab on a project's, or a line under an experiment's header.

Pages are not plugin surface (they are dlboard's own, `serve/_pages/`), with this one exception: a plugin that keeps a
setting of its own (`register_setting`) needs somewhere to show and change it, and the page of what it is set for is where
people look. A project's page gives each plugin a tab of its own, so the plugins a deployment adds never lengthen the page,
and each one's part of it is named (#615). Both render in the page's own `layout()`, so they cost no callback round trip to
draw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from dlboard._compat import StrEnum
from dlboard.serve._backend._app_slot import AppSlot, current_dash_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.models import Experiment, Project


class ProjectPageTab(StrEnum):
    """The project page's own tabs, by their `?tab=` value: a plugin's tab is named anything else."""

    EXPERIMENTS = "experiments"
    COMPARISONS = "comparisons"
    SHARING = "sharing"


_TAB_KEY: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
"""What a tab's key may be: it is the `?tab=` of a link to it."""


@dataclass(frozen=True)
class ProjectTab:
    """A plugin's tab on a project's page: `render` draws it for the project, or nothing (`None`: then there is no tab)."""

    key: str
    """Its `?tab=` value, so a link opens the page on it: lowercase letters, digits and dashes."""
    label: str
    render: Callable[[Project], Component | None]


class ExperimentSlot(StrEnum):
    """Where on an experiment's page a section goes."""

    HEADER = "header"
    """A line under the experiment's name and description."""


@dataclass(frozen=True)
class ExperimentSection:
    """A plugin's part of an experiment's page: `render` draws it for the experiment (or nothing, `None`) at `slot`."""

    slot: ExperimentSlot
    render: Callable[[Experiment], Component | None]


_PROJECT_TABS: AppSlot[list[ProjectTab]] = AppSlot("project tabs")
_EXPERIMENT_SECTIONS: AppSlot[list[ExperimentSection]] = AppSlot("experiment sections")


def register_project_tab(app: Dash, tab: ProjectTab) -> None:
    """Add `tab` to every project's page, after the tabs registered before it."""
    tabs = _PROJECT_TABS.setdefault(app, list)
    if not _TAB_KEY.fullmatch(tab.key):
        msg = f"A tab's key is its ?tab= value: lowercase letters, digits and dashes, not {tab.key!r}"
        raise ValueError(msg)
    if tab.key in set(ProjectPageTab) | {t.key for t in tabs}:
        msg = f"The project page already has a tab {tab.key!r}"
        raise ValueError(msg)
    tabs.append(tab)


def project_tabs(project: Project) -> list[tuple[ProjectTab, Component]]:
    """The tabs plugins add to `project`'s page, each with what it draws, in the order they registered them."""
    return [
        (tab, drawn)
        for tab in _PROJECT_TABS.find(current_dash_app()) or []
        if (drawn := tab.render(project)) is not None
    ]


def register_experiment_section(app: Dash, section: ExperimentSection) -> None:
    """Add `section` to every experiment's page, after the ones registered before it."""
    _EXPERIMENT_SECTIONS.setdefault(app, list).append(section)


def experiment_sections(slot: ExperimentSlot, experiment: Experiment) -> list[Component]:
    """The sections plugins add to `experiment`'s page at `slot`, in the order they registered them."""
    return [
        drawn
        for section in _EXPERIMENT_SECTIONS.find(current_dash_app()) or []
        if section.slot is slot and (drawn := section.render(experiment)) is not None
    ]
