"""A table says in a tooltip what a cell cut off: the table chart's cells, and the run table's names."""

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

_LONG = "a-very-long-optimizer-configuration-name-that-no-narrow-column-can-show-whole"
_RUN = "a-run-whose-name-is-far-too-long-for-the-sidebar-to-show-in-one-go"


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def test_a_cut_off_cell_says_what_it_holds_in_a_tooltip_and_one_that_shows_it_all_has_none(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name="Tooltip Project", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="tooltips"))
    run = store.create_run(models.NewRun(experiment_id=experiment.id, name=_RUN))
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment.id, {"optim": _LONG, "seed": 1}))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5},
                step=0,
                experiment_id=experiment.id,
                run_id=run.id,
                timestamp_utc=pendulum.now("UTC"),
            )
        ]
    )
    owner = store.get_or_create_user(models.Principal.unverified("tooltips"))
    table = models.ChartInstance[Any, Any](
        chart_type="table",
        parameters={"show_metrics": ["loss"], "show_hparams": ["optim", "seed"], "width": 360},
    )
    view = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment.id,
            owner_id=owner.id,
            name="Tooltips",
            panels=[models.PanelInstance[Any, Any](name="Table", charts=[table])],
        ),
    )

    page.goto(f"{live_server_url}/experiment/{experiment.id}?view={view.id}")

    chart = page.locator(".dl-table-chart")
    cut_off = chart.locator(".ag-cell[col-id='hparam__optim']")
    expect(cut_off).to_be_visible()
    cut_off.hover()
    expect(page.locator(".ag-tooltip")).to_have_text(_LONG)
    chart.locator(".ag-cell[col-id='hparam__seed']").hover()
    expect(page.locator(".ag-tooltip")).to_have_count(0)

    page.locator(".dl-run-table .ag-cell[col-id='run_name']").first.hover()
    expect(page.locator(".ag-tooltip")).to_have_text(_RUN)
    assert console_errors == []
