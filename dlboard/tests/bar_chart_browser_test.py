"""A bar chart draws every visible run as a bar of its own, in the run's color (#493)."""

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

_HIDDEN_SIZES = (64, 64, 128, 256)
_BAR = ".recharts-bar-rectangle path"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def test_a_bar_chart_draws_each_run_as_a_bar_of_its_own_in_the_group_of_its_hyperparameter(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name="Bar Chart Project", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="sweep"))
    for number, hidden in enumerate(_HIDDEN_SIZES, start=1):
        run = store.create_run(models.NewRun(experiment_id=experiment.id, name=f"seed-{number}"))
        store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment.id, {"hidden": hidden}))
        store.log_metrics(
            models.LoggedMetrics(
                metrics={"loss": 1.0 / (step + number)},
                step=step,
                experiment_id=experiment.id,
                run_id=run.id,
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(3)
        )
    owner = store.get_or_create_user(models.Principal.unverified("bars"))
    view = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment.id,
            owner_id=owner.id,
            name="Bars",
            panels=[
                models.PanelInstance[Any, Any](
                    name="Bars",
                    charts=[
                        models.ChartInstance(
                            chart_type="bar", parameters={"column": "loss", "x_axis": "hidden"}
                        )
                    ],
                )
            ],
        ),
    )

    page.goto(f"{live_server_url}/experiment/{experiment.id}?view={view.id}")

    # Four groups' worth of runs, not three: the two runs that share a hidden size are two bars side by side.
    expect(page.locator(_BAR)).to_have_count(len(_HIDDEN_SIZES))
    expect(page.locator(".mantine-ChartLegend-legendItemName")).to_have_text(
        [f"seed-{number}" for number in range(1, len(_HIDDEN_SIZES) + 1)]
    )
    assert console_errors == []
