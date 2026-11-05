"""What plugins add to a project's page: each slot's sections, in the order they registered, and none that draws nothing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from dash import Dash, html

from dlboard import models
from dlboard.serve._backend._app_slot import DASH_APP_EXTENSION
from dlboard.serve._pages._project_sections import (
    ProjectSection,
    ProjectSlot,
    project_sections,
    register_project_section,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def app() -> Iterator[Dash]:
    app = Dash(__name__)
    app.server.extensions[DASH_APP_EXTENSION] = app
    with app.server.app_context():
        yield app


def test_a_slot_draws_its_sections_in_the_order_they_registered_and_skips_one_that_draws_nothing(
    app: Dash,
) -> None:
    project = models.Project(name="p", description="", id=1)
    for slot, text in (
        (ProjectSlot.BODY, "first"),
        (ProjectSlot.HEADER, "line"),
        (ProjectSlot.BODY, "second"),
    ):
        register_project_section(app, ProjectSection(slot, lambda p, text=text: html.Div(f"{text} {p.name}")))
    register_project_section(app, ProjectSection(ProjectSlot.BODY, lambda _p: None))

    assert [s.children for s in project_sections(ProjectSlot.BODY, project)] == ["first p", "second p"]
    assert [s.children for s in project_sections(ProjectSlot.HEADER, project)] == ["line p"]
