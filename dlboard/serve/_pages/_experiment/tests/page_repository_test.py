"""Saving an experiment page: written in place when it's yours, branched into a view of your own when it isn't."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from dlboard import models
from dlboard.models._view import PanelInstance
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _page_repository as repo

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_saving_document_settings_writes_your_own_view_in_place(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """Written straight into the page `ref` names, under the key the setting has always been stored by."""
    sign_in_as("alice")
    view = store.create_view(
        page_model.BasicExperimentPage,
        models.NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=store.get_or_create_user(models.Principal.unverified("alice")).id,
            name="mine",
        ),
    )

    page = repo.save_document_settings(
        store, repo.ExperimentRef(experiment_id, view.id), page_model.DocumentSettings(hparam_columns=["lr"])
    )

    assert page.document.hparam_columns == ["lr"]
    assert page.id == view.id  # written in place -- alice already owns this one
    reloaded = store.get_view(page_model.BasicExperimentPage, view.id)
    assert reloaded is not None
    assert reloaded.page_settings["hparam-table-selected"] == ["lr"]


def test_saving_document_settings_branches_into_your_own_view_when_editing_the_shared_page(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    """
    The shared page has no owner, so editing it while looking at it (`view_id=None`) can never
    write in place -- otherwise the very first edit anyone made from the shared page would change
    what everyone else sees.
    """
    sign_in_as("alice")

    page = repo.save_document_settings(
        store, repo.ExperimentRef(experiment_id, None), page_model.DocumentSettings(hparam_columns=["lr"])
    )

    assert page.document.hparam_columns == ["lr"]
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("alice")).id
    shared = cast(
        "page_model.BasicExperimentPage",
        store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id),
    )
    assert shared.id != page.id
    assert shared.page_settings == {}


def test_saving_document_settings_branches_into_a_separate_view_when_editing_someone_elses(
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
    page = repo.save_document_settings(
        store,
        repo.ExperimentRef(experiment_id, alices_view.id),
        page_model.DocumentSettings(hparam_columns=["lr"]),
    )

    assert page.id != alices_view.id
    assert page.owner_id == store.get_or_create_user(models.Principal.unverified("bob")).id
    assert page.panels == alices_view.panels  # seeded from what bob was looking at
    assert page.document.hparam_columns == ["lr"]
    untouched = store.get_view(page_model.BasicExperimentPage, alices_view.id)
    assert untouched is not None
    assert untouched.page_settings == {}


def _comparison(
    store: SQLLiteStore, project_id: int, owner: str, run_ids: set[int], name: str = "lr sweep"
) -> page_model.BasicExperimentPage:
    return cast(
        "page_model.BasicExperimentPage",
        store.create_comparison(
            page_model.BasicExperimentPage,
            models.NewPage[Any, Any](
                project_id=project_id,
                owner_id=store.get_or_create_user(models.Principal.unverified(owner)).id,
                name=name,
                shared=True,
                panels=[PanelInstance[Any, Any](name="p")],
            ),
            frozenset(run_ids),
        ),
    )


def _project_and_runs(store: SQLLiteStore, experiment_id: int) -> tuple[int, set[int]]:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    runs = {store.create_run(models.NewRun(experiment_id=experiment_id)).id for _ in range(2)}
    return experiment.project_id, runs


def test_a_page_knows_which_page_it_is(store: SQLLiteStore, experiment_id: int) -> None:
    project_id, _ = _project_and_runs(store, experiment_id)
    alice = store.get_or_create_user(models.Principal.unverified("alice"))
    shared = cast(
        "page_model.BasicExperimentPage",
        store.get_or_create_page(page_model.BasicExperimentPage, experiment_id=experiment_id),
    )
    view = cast(
        "page_model.BasicExperimentPage",
        store.create_view(
            page_model.BasicExperimentPage,
            models.NewPage[Any, Any](experiment_id=experiment_id, owner_id=alice.id, name="mine"),
        ),
    )
    comparison = _comparison(store, project_id, "alice", set())

    assert repo.ref_of(shared) == repo.ExperimentRef(experiment_id, None)
    assert repo.ref_of(view) == repo.ExperimentRef(experiment_id, view.id)
    assert repo.ref_of(comparison) == repo.ComparisonRef(comparison.id)
    for ref, page in ((repo.ref_of(view), view), (repo.ref_of(comparison), comparison)):
        assert repo.load_page(store, ref).id == page.id


def test_a_comparison_that_is_gone_is_an_error_not_another_page(
    store: SQLLiteStore, experiment_id: int
) -> None:
    project_id, _ = _project_and_runs(store, experiment_id)
    comparison = _comparison(store, project_id, "alice", set())
    store.delete_view(comparison.id, comparison.owner_id or 0)

    with pytest.raises(LookupError, match="is gone"):
        repo.load_page(store, repo.ComparisonRef(comparison.id))


def test_editing_a_comparison_of_your_own_writes_it_in_place_and_anyone_elses_forks_a_copy_with_its_runs(
    store: SQLLiteStore, experiment_id: int, sign_in_as: Callable[[str], None]
) -> None:
    project_id, runs = _project_and_runs(store, experiment_id)
    alices = _comparison(store, project_id, "alice", runs)
    change = page_model.DocumentSettings(hparam_columns=["lr"])
    ref = repo.ComparisonRef(alices.id)

    sign_in_as("alice")
    assert repo.save_document_settings(store, ref, change).id == alices.id

    sign_in_as("bob")
    bob = store.get_or_create_user(models.Principal.unverified("bob"))
    forked = repo.save_document_settings(store, ref, change)
    again = repo.save_document_settings(
        store, repo.ref_of(forked), page_model.DocumentSettings(hparam_columns=["wd"])
    )
    second = repo.save_document_settings(store, ref, change)

    assert (forked.owner_id, forked.name, forked.project_id) == (bob.id, "My comparison", project_id)
    assert forked.panels == alices.panels
    assert {run.id for run in store.comparison_runs(forked.id)} == runs
    assert again.id == forked.id  # bob's own now: written in place
    assert second.name == "My comparison 2"
    assert repo.load_page(store, ref).document.hparam_columns == [
        "lr"
    ]  # alice's own edit, and nothing of bob's


def test_a_shared_page_of_a_project_is_no_comparison_and_cannot_be_made(
    store: SQLLiteStore, experiment_id: int
) -> None:
    """The one rule that says what a project's page is: someone's comparison. The store keeps it, and `ref_of` says so."""
    project_id, _ = _project_and_runs(store, experiment_id)
    ownerless = page_model.BasicExperimentPage(id=1, project_id=project_id, owner_id=None, name="")

    with pytest.raises(ValueError, match="are comparisons"):
        store.get_or_create_page(page_model.BasicExperimentPage, project_id=project_id)
    with pytest.raises(ValueError, match="shared page of a project"):
        repo.ref_of(ownerless)
