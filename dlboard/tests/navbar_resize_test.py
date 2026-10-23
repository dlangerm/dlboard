"""Dragging the navbar's edge resizes it, and never past the width bounds `app.py` serves the drag script with."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve._constants import NAVBAR_ID, NAVBAR_RESIZE_HANDLE_ID

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

VIEWPORT_WIDTH = 1280


@pytest.mark.parametrize(
    ("drag_to", "width"),
    [(400, 400), (50, 260), (VIEWPORT_WIDTH - 50, 640)],
    ids=["inside the bounds", "narrower than the minimum", "wider than the maximum"],
)
def test_dragging_the_navbar_edge_resizes_it_within_the_bounds(
    page: Page, live_server_url: str, drag_to: int, width: int
) -> None:
    api = BasicDlboardAPI(live_server_url)
    project = api.create_project(models.NewProject(name="Resize Project", description=""))
    experiment = api.create_experiment(models.NewExperiment(project_id=project.id, name="Resize Experiment"))
    page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": 800})
    page.goto(f"{live_server_url}/experiment/{experiment.id}")

    handle = page.locator(f"#{NAVBAR_RESIZE_HANDLE_ID}").bounding_box()
    assert handle is not None
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + 100)
    page.mouse.down()
    page.mouse.move(drag_to, handle["y"] + 100, steps=5)
    page.mouse.up()

    navbar = page.locator(f"#{NAVBAR_ID}").bounding_box()
    assert navbar is not None
    assert navbar["width"] == pytest.approx(width, abs=1)
