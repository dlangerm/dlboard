"""A sweep's scatter, parallel coordinates and grouped lines draw in the browser, as themed figures (#698, #699)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_LEARNING_RATES = (0.1, 0.01, 0.001)


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def test_a_sweep_draws_a_point_per_run_and_a_line_per_run_across_its_hyperparameters(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name="Sweep Project", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="lr sweep"))
    for number, lr in enumerate(_LEARNING_RATES):
        run = store.create_run(models.NewRun(experiment_id=experiment.id, name=f"lr-{lr}"))
        store.log_hyperparams(
            models.NewHyperParams.from_raw(
                run.id, experiment.id, {"lr": lr, "optimizer": ("adam", "sgd")[number % 2]}
            )
        )
        store.log_metrics(
            [
                models.LoggedMetrics(
                    metrics={"loss": 1.0 / (number + 1)},
                    step=0,
                    experiment_id=experiment.id,
                    run_id=run.id,
                    timestamp_utc=pendulum.now("UTC"),
                )
            ]
        )
    owner = store.get_or_create_user(models.Principal.unverified("sweeper"))
    view = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment.id,
            owner_id=owner.id,
            name="Sweep",
            panels=[
                models.PanelInstance[Any, Any](
                    name="Sweep",
                    charts=[
                        models.ChartInstance(chart_type="scatter", parameters={"x": "lr", "y": "loss"}),
                        models.ChartInstance(
                            chart_type="parallel_coordinates", parameters={"metric": "loss"}
                        ),
                        models.ChartInstance(
                            chart_type="line",
                            parameters={"column": "loss", "x_axis": "step", "group_by": "optimizer"},
                        ),
                    ],
                )
            ],
        ),
    )

    page.goto(f"{live_server_url}/experiment/{experiment.id}?view={view.id}")

    expect(page.locator(".scatterlayer .point")).to_have_count(len(_LEARNING_RATES), timeout=15_000)
    expect(page.locator(".parcoords .axis-title")).to_have_text(["lr", "optimizer", "loss"])
    # The grouped line chart (#699): a legend entry per value, saying how many runs it is the mean of.
    expect(page.locator(".legendtext")).to_have_text(["optimizer=adam (2 runs)", "optimizer=sgd (1 runs)"])
    assert console_errors == []
