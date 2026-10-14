"""Saving an experiment page: written in place when it's yours, branched into a view of your own when it isn't."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dlboard import models
from dlboard.models._view import PanelInstance
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_persist_settings_merges_into_page_settings_without_touching_panels(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Unlike `_persist_settings_and_rerender`, this must not need/trigger an accordion rebuild."""
    sign_in_as("alice")
    view = store.create_view(
        page_model.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="mine",
        ),
    )

    page = repo.persist_settings(store, repo.PageRef(experiment_id, view.id), {"selected": ["lr"]})

    assert page.page_settings["selected"] == ["lr"]
    assert page.id == view.id  # written in place -- alice already owns this one
    reloaded = store.get_view(page_model.BasicExperimentPage, view.id)
    assert reloaded is not None
    assert reloaded.page_settings["selected"] == ["lr"]


def test_persist_settings_branches_into_your_own_view_when_editing_the_shared_page(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """
    The shared page has no owner, so editing it while looking at it (`view_id=None`) can never
    write in place -- otherwise the very first edit anyone made from the shared page would change
    what everyone else sees.
    """
    sign_in_as("alice")

    page = repo.persist_settings(store, repo.PageRef(experiment_id, None), {"selected": ["lr"]})

    assert page.page_settings["selected"] == ["lr"]
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("alice")).id
    shared = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    assert shared.id != page.id
    assert shared.page_settings == {}


def test_persist_settings_branches_into_a_separate_view_when_editing_someone_elses(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Bob editing alice's view must never change what alice sees -- it forks a view of bob's own."""
    sign_in_as("alice")
    alices_view = store.create_view(
        page_model.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="alice's view",
            panels=[PanelInstance[Any, Any](name="p")],
        ),
    )

    sign_in_as("bob")
    page = repo.persist_settings(store, repo.PageRef(experiment_id, alices_view.id), {"selected": ["lr"]})

    assert page.id != alices_view.id
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("bob")).id
    assert page.panels == alices_view.panels  # seeded from what bob was looking at
    assert page.page_settings["selected"] == ["lr"]
    untouched = store.get_view(page_model.BasicExperimentPage, alices_view.id)
    assert untouched is not None
    assert untouched.page_settings == {}


def test_persist_settings_can_opt_out_of_branching_for_purely_personal_viewing_state(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Which panel/tab is open is shared, last-write-wins state -- opening one never forks a view."""
    sign_in_as("alice")
    store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)

    page = repo.persist_settings(
        store, repo.PageRef(experiment_id, None), {"open_panel": ["p"]}, branch_on_edit=False
    )

    assert page.owner_id is None
    shared = store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id)
    assert shared.page_settings["open_panel"] == ["p"]
