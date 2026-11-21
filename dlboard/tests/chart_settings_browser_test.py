"""A panel's chart settings drawing its charts, a locked chart keeping its own, and settings written into filtered charts."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

import pendulum
import pytest
from playwright.sync_api import expect

from dlboard import models
from dlboard.client._rest_api import BasicDlboardAPI
from dlboard.serve import get_system_data_store
from dlboard.serve._pages._experiment._chart_editor import CHART_EDITOR_SAVE_ID
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
from dlboard.tests.browser_test import _session_app_is_current  # noqa: F401

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

pytestmark = pytest.mark.browser


def _line_charts(page: Page, live_server_url: str, metrics: tuple[str, ...] = ("loss", "acc")) -> Locator:
    """The shared page of an experiment whose one panel ("Losses") draws a line chart of each of `metrics`, open; its charts."""
    store = get_system_data_store()
    project = store.create_project(models.NewProject(name=f"Settings {time.monotonic_ns()}", description=""))
    experiment = store.create_experiment(models.NewExperiment(project_id=project.id, name="layered"))
    api = BasicDlboardAPI(live_server_url)
    run = api.create_run(models.NewRun(experiment_id=experiment.id))
    api.log_metric_batch(
        [
            models.LoggedMetrics(
                experiment_id=experiment.id,
                run_id=run.id,
                step=step,
                metrics=dict.fromkeys(metrics, 1.0 / (step + 1)),
                timestamp_utc=pendulum.now("UTC"),
            )
            for step in range(5)
        ]
    )
    charts = [
        models.ChartInstance[Any, Any](chart_type="line", parameters={"column": metric, "x_axis": "step"})
        for metric in metrics
    ]
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment.id)
    store.update_page(
        shared.model_copy(update={"panels": [models.PanelInstance[Any, Any](name="Losses", charts=charts)]})
    )
    page.goto(f"{live_server_url}/experiment/{experiment.id}")
    cards = page.locator(".dl-chart-item")
    expect(cards.first).to_be_visible(timeout=15_000)
    return cards


def _chart_settings(page: Page) -> Locator:
    page.get_by_role("button", name="Panel actions").first.click()
    page.get_by_role("menuitem", name="Chart settings…").click()
    dialog = page.get_by_role("dialog", name="Chart settings")
    expect(dialog).to_be_visible()
    return dialog


def _edit_chart(page: Page, card: Locator) -> Locator:
    """Open the chart editor on `card`, on its Display section; the editor's pane."""
    card.hover()
    card.get_by_role("button", name="Edit chart").click()
    editor = page.get_by_role("complementary", name="Chart settings")
    editor.get_by_role("button", name="Display").click()
    return editor


def _height(card: Locator) -> float:
    """How tall `card`'s chart is drawn: its draft, while the chart editor has one over it; 0 while it is drawn again."""
    for part in (".dl-chart-draft", ".dl-chart-body"):
        box = card.locator(part).bounding_box()
        if box and box["height"]:
            return box["height"]
    return 0.0


def _drawn_taller(page: Page, card: Locator, than: float) -> bool:
    """Whether `card`'s chart is drawn taller than `than` px within a few seconds (it is drawn again after an edit)."""
    for _ in range(50):
        if _height(card) > than:
            return True
        page.wait_for_timeout(100)
    return False


def _saved_view(page: Page) -> BasicExperimentPage:
    expect(page).to_have_url(re.compile(r"[?&]view=\d+"))
    saved = get_system_data_store().get_view(BasicExperimentPage, int(re.findall(r"view=(\d+)", page.url)[0]))
    assert isinstance(saved, BasicExperimentPage)
    return saved


def test_a_panels_chart_settings_draw_its_charts_but_a_locked_one_keeps_its_own(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    cards = _line_charts(page, live_server_url)
    default = _height(cards.first)

    dialog = _chart_settings(page)
    expect(dialog).to_contain_text("Every chart in Losses· 2 charts")
    expect(dialog).to_contain_text("2 line charts")
    dialog.get_by_role("textbox", name="Height").fill("520")
    expect(
        dialog.get_by_role("checkbox", name="Set Height")
    ).to_be_checked()  # a setting changed is one to set
    dialog.get_by_role("button", name="Apply").click()
    expect(dialog).to_be_hidden()

    saved = _saved_view(page)  # the shared page's panel, in a view of your own
    assert saved.panels[0].chart_settings == {"height": 520}
    assert all("height" not in chart.parameters for chart in saved.panels[0].charts)  # drawn, never saved
    assert _drawn_taller(page, cards.first, default + 150)

    cards.first.hover()
    cards.first.get_by_role("button", name=re.compile("^Lock")).click()
    expect(cards.first.get_by_role("button", name=re.compile("^Locked"))).to_be_visible()  # without a hover
    assert not _drawn_taller(page, cards.first, default + 50)  # its own height again
    assert _height(cards.nth(1)) > default + 150  # the other one still follows its panel
    locked = _saved_view(page).panels[0].charts[0]
    assert locked.locked

    editor = _edit_chart(page, cards.nth(1))
    expect(editor.get_by_role("textbox", name="Height")).to_have_value("520")  # as it is drawn
    expect(editor).to_contain_text("Set by the panel's chart settings")
    assert console_errors == []


def test_editing_a_setting_its_panel_sets_locks_the_chart_with_the_edit_and_any_other_edit_does_not(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    cards = _line_charts(page, live_server_url)
    store = get_system_data_store()
    shared = store.get_or_create_page(
        BasicExperimentPage, experiment_id=int(re.findall(r"/experiment/(\d+)", page.url)[0])
    )
    panel = shared.panels[0].model_copy(update={"chart_settings": {"height": 520}})
    store.update_page(shared.model_copy(update={"panels": [panel]}))
    page.reload()

    editor = _edit_chart(page, cards.nth(1))
    smoothing = editor.get_by_role("slider", name="Smoothing")
    smoothing.focus()
    for _ in range(5):
        page.keyboard.press("ArrowRight")
    expect(smoothing).to_have_attribute("aria-valuenow", "0.05")
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()
    expect(editor).to_be_hidden()
    other = _saved_view(page).panels[0].charts[1]
    assert not other.locked  # still drawn by its panel's height
    assert "height" not in other.parameters
    assert other.parameters["smoothing"] == pytest.approx(0.05)

    drawn = _height(cards.first)
    editor = _edit_chart(page, cards.first)
    editor.get_by_role("textbox", name="Height").fill("640")
    assert _drawn_taller(page, cards.first, drawn + 80)  # its draft, as it will be saved: locked, at 640
    page.locator(f"#{CHART_EDITOR_SAVE_ID}").click()
    expect(editor).to_be_hidden()
    edited = _saved_view(page).panels[0].charts[0]
    assert edited.locked  # else its panel's 520 would be drawn over the 640 asked for
    assert edited.parameters["height"] == 640
    expect(cards.first.get_by_role("button", name=re.compile("^Locked"))).to_be_visible()
    assert _height(cards.first) > _height(cards.nth(1)) + 80
    assert console_errors == []


def test_a_setting_no_chart_of_the_panel_has_is_not_offered_but_kept_for_one_added_later(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    """The panel's smoothing outlives its last line chart: a line chart added back is drawn with it."""
    _line_charts(page, live_server_url)
    store = get_system_data_store()
    experiment_id = int(re.findall(r"/experiment/(\d+)", page.url)[0])
    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)
    bar = models.ChartInstance[Any, Any](chart_type="bar", parameters={"column": "loss", "x_axis": "step"})
    panel = models.PanelInstance[Any, Any](name="Losses", charts=[bar], chart_settings={"smoothing": 0.6})
    store.update_page(shared.model_copy(update={"panels": [panel]}))
    page.reload()

    dialog = _chart_settings(page)
    expect(dialog.get_by_role("checkbox", name="Set Height")).to_be_visible()
    expect(dialog.get_by_role("checkbox", name="Set Smoothing")).to_have_count(
        0
    )  # no line chart: not offered
    dialog.get_by_role("textbox", name="Height").fill("420")
    dialog.get_by_role("button", name="Apply").click()
    expect(dialog).to_be_hidden()

    saved = _saved_view(page).panels[0]
    assert saved.chart_settings == {"smoothing": 0.6, "height": 420}  # kept, though nothing here draws it
    line = models.ChartInstance[Any, Any](chart_type="line", parameters={"column": "loss", "x_axis": "step"})
    added = saved.model_copy(update={"charts": [*saved.charts, line]}).drawn({})
    assert added.charts[1].parameters["smoothing"] == 0.6  # a line chart added later is drawn with it
    assert console_errors == []


def test_with_the_chart_filter_typed_the_settings_are_written_into_the_charts_it_matches(
    page: Page, live_server_url: str, console_errors: list[str]
) -> None:
    families = tuple(f"{family}_{letter}" for family in ("loss", "acc") for letter in "abcdef")
    cards = _line_charts(
        page, live_server_url, families
    )  # more than a page of them: the panel has a chart filter
    page.get_by_placeholder("Filter charts").fill("acc")
    expect(cards).to_have_count(6)

    dialog = _chart_settings(page)
    expect(dialog).to_contain_text("Charts in Losses matching “acc”· 6 charts")
    expect(dialog).to_contain_text("written into each of these charts, once")
    dialog.get_by_role("checkbox", name="Set Height").check()
    dialog.get_by_role("textbox", name="Height").fill("450")
    dialog.get_by_role("button", name="Apply").click()
    expect(dialog).to_be_hidden()

    panel = _saved_view(page).panels[0]
    assert panel.chart_settings == {}  # written into the charts, not the panel's
    assert [chart.parameters.get("height") for chart in panel.charts] == [None] * 6 + [450] * 6
    assert console_errors == []
