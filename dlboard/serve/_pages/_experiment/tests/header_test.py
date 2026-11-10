"""The experiment page's header, drawn from the experiment it shows: what is on it, and whose view it says it is."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dlboard import models
from dlboard.conftest import find_props, props
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._experiment import _page_repository as repo
from dlboard.serve._pages._experiment._header import (
    EXPERIMENT_DELETE_IDS,
    EXPERIMENT_HEADER_IDS,
    experiment_header,
)
from dlboard.serve._pages._experiment._live_updates import LIVE_UPDATES_ENABLED_ID
from dlboard.serve._pages._experiment._notes import NOTES_OPEN_ID
from dlboard.serve._pages._experiment._views import VIEW_MENU_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.models import User
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_the_header_shows_the_experiment_its_actions_and_the_page_controls(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], User]
) -> None:
    author = sign_in_as("alice")
    store.add_comment(models.NewComment(experiment_id=experiment_id, author_id=author.id, body="hi"))
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    page = repo.load_page(store, repo.ExperimentRef(experiment_id, None))

    header = experiment_header(store, experiment, page, None)

    assert (find_props(header, EXPERIMENT_HEADER_IDS.title) or {})["children"] == experiment_display_name(
        experiment
    )
    for drawn in (EXPERIMENT_HEADER_IDS.edit_button, EXPERIMENT_DELETE_IDS.button, LIVE_UPDATES_ENABLED_ID):
        assert find_props(header, drawn) is not None
    notes = find_props(header, NOTES_OPEN_ID) or {}
    assert props(notes["rightSection"])["children"] == "1"  # how many notes, on the button that opens them


@pytest.mark.parametrize(("shown", "owned"), [("shared", False), ("mine", True), ("theirs", False)])
def test_only_a_view_of_your_own_offers_its_owner_actions(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], User], shown: str, *, owned: bool
) -> None:
    shared = repo.load_page(store, repo.ExperimentRef(experiment_id, None))
    sign_in_as("bob")
    theirs = repo.save_page(store, shared)
    sign_in_as("alice")
    mine = repo.save_page(store, shared)
    page = {"shared": shared, "mine": mine, "theirs": theirs}[shown]
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None

    header = experiment_header(store, experiment, page, None if shown == "shared" else page.id)

    assert ((find_props(header, VIEW_MENU_ID) or {})["className"] == "dl-view-owned") is owned
