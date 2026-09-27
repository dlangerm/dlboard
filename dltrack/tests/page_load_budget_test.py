"""
A hard ceiling on how many server callbacks each page's initial load costs.

Every `_dash-update-component` request is a serial round trip the user waits on before a page is
fully drawn, so this is the regression guard for "performance is paramount": a change that adds
one to a page's load fails here, and one that removes one should lower that page's budget in the
same change. Interval-driven callbacks (the experiment page's live-update poll) aren't part of a
page's load, so they're excluded rather than raced against.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pendulum
import pytest
from pydantic import BaseModel

from dltrack import models
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request

pytestmark = pytest.mark.browser


class _Route(StrEnum):
    HOME = "home"
    PROJECT = "project"
    EXPERIMENT = "experiment"


PAGE_LOAD_CALLBACK_BUDGET: dict[_Route, int] = {
    _Route.HOME: 4,
    _Route.PROJECT: 7,
    _Route.EXPERIMENT: 10,
}
"""The most `_dash-update-component` requests each page's initial load may issue. Only ever lower."""


class _CallbackRequest(BaseModel):
    """The two fields of a `_dash-update-component` request body this test reads."""

    output: str
    changedPropIds: list[str] = []  # noqa: N815 -- Dash's own wire name


def _load_callbacks(page: Page, url: str) -> list[str]:
    """Load `url` and return the output ids of every non-interval callback it triggered."""
    outputs: list[str] = []

    def record(request: Request) -> None:
        if "_dash-update-component" not in request.url or request.post_data is None:
            return
        body = _CallbackRequest.model_validate_json(request.post_data)
        if not any(prop.endswith(".n_intervals") for prop in body.changedPropIds):
            outputs.append(body.output)

    page.on("request", record)
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.remove_listener("request", record)
    return outputs


def test_each_page_load_stays_within_its_callback_budget(page: Page, live_server_url: str) -> None:
    api = BasicDltrackAPI(live_server_url)
    project = api.create_project(models.NewProject(name="Budget Project", description=""))
    experiment = api.create_experiment(models.NewExperiment(project_id=project.id, name="Budget Experiment"))
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment.id,
                run_id=run.id,
                step=step,
                metrics={"loss": 1.0 / (step + 1)},
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        ]
    )
    urls = {
        _Route.HOME: live_server_url,
        _Route.PROJECT: f"{live_server_url}/project/{project.id}",
        _Route.EXPERIMENT: f"{live_server_url}/experiment/{experiment.id}",
    }

    loads = {route: _load_callbacks(page, url) for route, url in urls.items()}

    over_budget = [
        f"{route}: {len(outputs)} > {PAGE_LOAD_CALLBACK_BUDGET[route]}\n  " + "\n  ".join(outputs)
        for route, outputs in loads.items()
        if len(outputs) > PAGE_LOAD_CALLBACK_BUDGET[route]
    ]
    assert not over_budget, "\n".join(over_budget)
