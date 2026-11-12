"""What plugins add to a project's or an experiment's page: tabs and sections in the order they registered, never one that draws nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dash import Dash, html

from dlboard import models
from dlboard.serve._backend._app_slot import DASH_APP_EXTENSION
from dlboard.serve._pages._page_sections import (
    ExperimentSection,
    ExperimentSlot,
    ProjectTab,
    experiment_sections,
    project_tabs,
    register_experiment_section,
    register_project_tab,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def app() -> Iterator[Dash]:
    app = Dash(__name__)
    app.server.extensions[DASH_APP_EXTENSION] = app
    with app.server.app_context():
        yield app


def _drawing(text: str) -> ProjectTab:
    return ProjectTab(key=text, label=text.title(), render=lambda p: html.Div(f"{text} {p.name}"))


def test_a_project_page_has_its_plugins_tabs_in_the_order_they_registered_but_not_one_that_draws_nothing(
    app: Dash,
) -> None:
    project = models.Project(name="p", description="", id=1)
    register_project_tab(app, _drawing("first"))
    register_project_tab(app, ProjectTab(key="empty", label="Empty", render=lambda _p: None))
    register_project_tab(app, _drawing("second"))

    assert [(tab.label, drawn.children) for tab, drawn in project_tabs(project)] == [
        ("First", "first p"),
        ("Second", "second p"),
    ]


@pytest.mark.parametrize(
    "key",
    ["experiments", "sharing", "first", "Has Spaces", "-leading-dash", ""],
    ids=[
        "the-pages-own",
        "the-pages-own-sharing",
        "another-plugins",
        "not-a-url-value",
        "a-leading-dash",
        "none",
    ],
)
def test_a_tab_is_refused_a_key_the_page_or_another_plugin_has_or_one_a_link_cannot_carry(
    app: Dash, key: str
) -> None:
    register_project_tab(app, _drawing("first"))

    with pytest.raises(ValueError, match="tab"):
        register_project_tab(app, _drawing(key))


def test_an_experiment_draws_its_own_sections_never_a_projects_tabs(app: Dash) -> None:
    experiment = models.Experiment(project_id=1, name="e", id=2)
    register_project_tab(app, _drawing("project"))
    register_experiment_section(
        app, ExperimentSection(ExperimentSlot.HEADER, lambda e: html.Div(f"experiment {e.name}"))
    )
    register_experiment_section(app, ExperimentSection(ExperimentSlot.HEADER, lambda _e: None))

    assert [s.children for s in experiment_sections(ExperimentSlot.HEADER, experiment)] == ["experiment e"]
