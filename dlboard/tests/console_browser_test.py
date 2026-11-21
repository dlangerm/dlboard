"""A run's console in the browser: its newest chunk first, older ones a click away, drawn as a terminal or log viewer would."""

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


def _console_of(
    live_server_url: str,
    tmp_path: Path,
    chunks: tuple[str, ...],
    settings: dict[str, Any] | None = None,
    runs: tuple[str, ...] = ("chatty",),
    **panel: Any,  # noqa: ANN401
) -> str:
    """
    The address of a view of one console chart of `settings` (in a panel of `panel`'s), of runs named `runs` (created in
    that order) that each printed `chunks`.
    """
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name=f"Console {time.monotonic_ns()}", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="printed"))
    api = BasicDlboardAPI(live_server_url)
    for name in runs:
        run = api.create_run(models.NewRun(experiment_id=experiment.id, name=name))
        staged = tmp_path / name
        staged.mkdir()
        api.log_artifact_batch(
            [
                ConsoleChunk(data=text.encode(), step=step).to_artifact(staged, run.id, experiment.id)
                for step, text in enumerate(chunks)
            ]
        )
    deadline = time.monotonic() + 30
    while len(list(store.fetch_artifacts(experiment.id, run_ids=None))) < len(chunks) * len(runs):
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
                        models.ChartInstance(
                            chart_type="console", parameters={"key": models.CONSOLE_KEY, **(settings or {})}
                        )
                    ],
                    **panel,
                )
            ],
        ),
    )
    return f"{live_server_url}/experiment/{experiment.id}?view={view.id}"


def test_the_run_picker_lists_the_newest_run_first_each_in_its_colours(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path
) -> None:
    """A run is found as the other charts draw it, and the one still printing is the one opened, as the Runs table lists them."""
    page.goto(_console_of(live_server_url, tmp_path, ("epoch 0\n",), runs=("first", "second", "third")))

    picker = page.get_by_role("textbox", name="Run", exact=True)
    expect(picker).to_have_value("third", timeout=15_000)
    picked_swatch = page.locator(".mantine-Select-section .dl-swatch")
    expect(picked_swatch).to_have_count(1)
    picker.click()
    options = page.get_by_role("option")
    expect(options).to_have_text(["third", "second", "first"])
    expect(options.locator(".dl-swatch")).to_have_count(3)
    swatches: list[str] = options.locator(".dl-swatch").evaluate_all(
        "spans => spans.map(span => span.className)"
    )
    assert len(set(swatches)) == 3  # each run its own colour
    options.nth(2).click()
    expect(picker).to_have_value("first")
    expect(picked_swatch).to_have_class(swatches[2])
    assert console_errors == []


def test_a_runs_console_opens_newest_first_pages_through_its_chunks_and_filters_their_lines(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path
) -> None:
    page.goto(_console_of(live_server_url, tmp_path, _CHUNKS))

    lines, position = page.locator(".dl-console-line"), page.locator(".dl-console-position")
    # The page's panels, then the chunk the browser fetches itself: on a cold server, more than the default 5 s.
    expect(lines).to_have_text(["epoch 2: loss 0.5"], timeout=15_000)
    expect(position).to_have_text("Chunk 3 of 3 (newest)")
    page.get_by_role("button", name="← Older").click()
    # Newest first: the chunk's last line at the top, so reading down goes back in time.
    expect(lines.nth(1)).to_have_text("epoch 1: loss 1.0")
    expect(position).to_have_text("Chunk 2 of 3")
    warning = lines.first
    expect(warning.locator(".dl-console-level")).to_have_text("WARNING ")
    expect(warning.locator(".dl-console-message")).to_have_text("loss went up")
    expect(warning.locator(".dl-console-rest")).to_have_text(" lr=0.1")
    page.get_by_placeholder("Filter these lines").fill("went up")
    expect(lines).to_have_count(1)
    page.get_by_placeholder("Filter these lines").fill("")
    page.get_by_role("button", name="← Older").click()
    expect(lines).to_have_text(["epoch 0: loss 2.0"])
    expect(position).to_have_text("Chunk 1 of 3 (oldest)")
    page.get_by_role("button", name="Newest ⇥").click()
    expect(lines).to_have_text(["epoch 2: loss 0.5"])
    page.get_by_role("button", name="⇤ Oldest").click()
    expect(lines).to_have_text(["epoch 0: loss 2.0"])
    page.get_by_role("button", name="Newer →").click()
    expect(lines).to_have_count(2)
    assert console_errors == []


@pytest.mark.parametrize("wrap", [True, False])
def test_a_line_wider_than_the_chart_wraps_onto_rows_unless_set_not_to(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path, *, wrap: bool
) -> None:
    """#734: a long line was cut at the chart's edge, the rest of it a sideways scroll away."""
    long = " ".join(f"\x1b[1mword\x1b[0m{n}" for n in range(200))
    settings = {"wrap": wrap, "order": "oldest_first"}
    page.goto(_console_of(live_server_url, tmp_path, (f"short\n{long}\n",), settings))

    lines, console = page.locator(".dl-console-line"), page.locator(".dl-console")
    expect(lines.first).to_have_text("short", timeout=15_000)
    rows = lines.evaluate_all("rows => rows.slice(1).map(row => row.textContent)")
    assert "".join(rows) == " ".join(f"word{n}" for n in range(200))
    assert (len(rows) > 1) is wrap
    assert console.evaluate("console => console.scrollWidth > console.clientWidth") is not wrap
    expect(lines.nth(1).locator(".dl-ansi-bold").first).to_have_text("word")
    assert console_errors == []


def test_newer_opens_a_chunk_read_newest_first_at_its_end_where_reading_goes_on(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path
) -> None:
    """#734: Newer opened a chunk at its top, and its first line, which goes on from the chunk read before, at its end."""
    chunks = tuple("".join(f"chunk {chunk} line {n}\n" for n in range(100)) for chunk in range(2))
    page.goto(_console_of(live_server_url, tmp_path, chunks))

    lines, console = page.locator(".dl-console-line"), page.locator(".dl-console")
    expect(lines.first).to_have_text("chunk 1 line 99", timeout=15_000)
    page.get_by_role("button", name="← Older").click()
    expect(lines.first).to_have_text("chunk 0 line 99")  # reading on down from the newer chunk's oldest line
    page.get_by_role("button", name="Newer →").click()
    expect(lines.last).to_have_text("chunk 1 line 0")
    assert console.evaluate("console => console.scrollTop + console.clientHeight >= console.scrollHeight - 1")
    assert console_errors == []


def test_a_long_chunk_read_oldest_first_is_drawn_a_screenful_at_a_time_in_its_colours_filling_its_cell(
    page: Page, live_server_url: str, console_errors: list[str], tmp_path: Path
) -> None:
    """#726: every line was a component of its own, escapes showed as text, and the chart kept its own width."""
    progress = "".join(f"\r\x1b[2Kepoch 1: {percent}%" for percent in range(0, 101, 10))
    chunk = f"\x1b[1;31mfailed\x1b[0m twice\n{progress}\n" + "".join(f"line {n}\n" for n in range(5000))
    oldest_first = {"order": "oldest_first"}
    page.goto(_console_of(live_server_url, tmp_path, (chunk,), oldest_first, layout="grid", grid_columns=1))

    lines = page.locator(".dl-console-line")
    expect(lines.first).to_have_text("failed twice", timeout=15_000)
    expect(lines.first.locator(".dl-ansi-fg-1.dl-ansi-bold")).to_have_text("failed")
    expect(lines.nth(1)).to_have_text("epoch 1: 100%")
    assert lines.count() < 100
    console = page.locator(".dl-console")
    console.evaluate("element => { element.scrollTop = element.scrollHeight; }")
    expect(lines.last).to_have_text("line 4999")
    assert lines.count() < 100
    cell, chart = page.locator(".dl-chart-item").bounding_box(), console.bounding_box()
    assert cell is not None
    assert chart is not None
    assert chart["width"] > cell["width"] - 40  # the cell's padding, not the 760 px the chart packs to
    assert console_errors == []
