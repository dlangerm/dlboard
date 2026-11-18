"""A run's console in the browser: its newest chunk first, older ones a click away, JSON lines drawn as log lines (#697)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._console import ConsoleChunk
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from dash import Dash
    from playwright.sync_api import Page

pytestmark = pytest.mark.browser

_CHUNKS = (
    "epoch 0: loss 2.0\n",
    "epoch 1: loss 1.0\n" + json.dumps({"event": "loss went up", "level": "warning", "lr": 0.1}) + "\n",
    "epoch 2: loss 0.5\n",
)


@pytest.fixture(autouse=True)
def _session_app_is_current(dlboard_app: Dash) -> Iterator[None]:
    """Read stores off `dlboard_app` in this module's helpers, as `browser_test.py` does."""
    with dlboard_app.server.app_context():
        yield


def test_a_runs_console_opens_on_its_newest_chunk_pages_back_and_filters_its_lines(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path
) -> None:
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name="Console Project", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="printed"))
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment.id, name="chatty"))
    api.log_artifact_batch(
        [
            ConsoleChunk(data=text.encode(), step=step).to_artifact(tmp_path, run.id, experiment.id)
            for step, text in enumerate(_CHUNKS)
        ]
    )
    deadline = time.monotonic() + 30
    while len(list(store.fetch_artifacts(experiment.id, run_ids=None))) < len(_CHUNKS):
        assert time.monotonic() < deadline, "the chunks were never recorded"
        time.sleep(0.1)
    owner = store.get_or_create_user(models.Principal.unverified("console"))
    view = store.create_view(
        BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment.id,
            owner_id=owner.id,
            name="Console",
            panels=[
                models.PanelInstance[Any, Any](
                    name="Console",
                    charts=[
                        models.ChartInstance(chart_type="console", parameters={"key": models.CONSOLE_KEY})
                    ],
                )
            ],
        ),
    )

    page.goto(f"{live_server_url}/experiment/{experiment.id}?view={view.id}")

    lines = page.locator(".dl-console-line")
    # The page's panels, then the chunk the browser fetches itself: on a cold server, more than the default 5 s.
    expect(lines).to_have_text(["epoch 2: loss 0.5"], timeout=15_000)
    page.get_by_role("button", name="Older").click()
    expect(lines).to_have_count(2)
    warning = page.locator(".dl-console-json")
    expect(warning.locator(".dl-console-level")).to_have_text("WARNING ")
    expect(warning.locator(".dl-console-message")).to_have_text("loss went up")
    expect(warning.locator(".dl-console-rest")).to_have_text(" lr=0.1")
    page.get_by_placeholder("Filter these lines").fill("went up")
    expect(lines).to_have_count(1)
    page.get_by_placeholder("Filter these lines").fill("")
    page.get_by_role("button", name="Older").click()
    expect(lines).to_have_text(["epoch 0: loss 2.0"])
    page.get_by_role("button", name="Newer").click()
    expect(lines).to_have_count(2)
    assert console_errors == []
