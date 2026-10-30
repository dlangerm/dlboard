"""The banner an experiment page shows when it was opened on one run: which run, and the way back to the view's own."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.conftest import find_props, props
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._focus import experiment_href, focus_banner
from dlboard.serve._pages._experiment._paging import Paging

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


@pytest.fixture(autouse=True)
def _plain_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dlboard.serve._pages._experiment._focus.relative_path", str)


def test_the_banner_names_the_run_and_links_back_to_the_view(store: SQLLiteStore, experiment_id: int) -> None:
    run = store.create_run(models.NewRun(experiment_id=experiment_id, name="fluffy-otter"))
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))

    banner = focus_banner(store, page, Paging().with_focus(run.id))

    assert banner is not None
    assert "fluffy-otter" in str(banner.to_plotly_json())
    assert find_props([banner], "focus-banner") is not None
    link = next(node for node in banner._traverse() if getattr(node, "href", None))
    assert props(link)["href"] == f"/experiment/{experiment_id}"


def test_there_is_no_banner_without_a_focus_or_for_a_run_that_is_not_the_experiments(
    store: SQLLiteStore, experiment_id: int
) -> None:
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))

    assert focus_banner(store, page, Paging()) is None
    assert focus_banner(store, page, Paging().with_focus(999_999)) is None


@pytest.mark.parametrize(
    ("view_id", "run_id", "expected"),
    [
        pytest.param(None, None, "/experiment/3", id="the-experiment"),
        pytest.param(None, 8, "/experiment/3?run=8", id="a-run"),
        pytest.param(5, 8, "/experiment/3?view=5&run=8", id="a-run-in-a-view"),
    ],
)
def test_a_link_to_an_experiment_keeps_its_view_and_run(
    view_id: int | None, run_id: int | None, expected: str
) -> None:
    assert experiment_href(3, view_id=view_id, run_id=run_id) == expected
