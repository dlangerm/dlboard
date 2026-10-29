"""
Where a page is read from and written to: an experiment's shared page or a named view of it, or a comparison.

The one place a page is saved. An edit to a page you don't own -- the shared page, someone else's view, or a comparison
that isn't yours -- branches into a copy of your own first (`save_page`), so nobody else's page ever changes under an
edit that isn't theirs.
"""

from __future__ import annotations

import itertools
import typing
from typing import TYPE_CHECKING, Any, cast

from dlboard import models
from dlboard.serve import get_current_user
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage, DocumentSettings

if TYPE_CHECKING:
    from collections.abc import Collection

    from dlboard.models import DataStore


class ExperimentRef(typing.NamedTuple):
    """
    Which page of an experiment a callback reads and writes: the shared one, or a named view of it.

    Every callback that loads the page by experiment builds one of these from `STATE_EXPERIMENT_ID`
    and `STATE_VIEW_ID`, so an edit made while looking at a view lands in that view, never the
    shared page. (Callbacks that already hold the page itself -- `STATE_PAGE_STORAGE` -- write it back
    by its own id, which is right either way, and take its ref from `ref_of`.)
    """

    experiment_id: int
    view_id: int | None


class ComparisonRef(typing.NamedTuple):
    """
    A comparison, which is its own page: there is no shared one for it to be a view of.

    A page scoped to a project is always a comparison, and always someone's: the store refuses a shared page of a project
    (`get_or_create_page`), so there is never an ownerless one for `ref_of` to take for one.
    """

    page_id: int


type PageRef = ExperimentRef | ComparisonRef
"""Whichever page an experiment-page callback reads and writes."""


def load_page(store: DataStore[...], ref: PageRef) -> BasicExperimentPage:
    """
    The page `ref` points at.

    An experiment's shared page if its view no longer exists or isn't this experiment's. A comparison that has gone, or
    that isn't yours to see, is an error: there is no page to fall back to.
    """
    match ref:
        case ExperimentRef(experiment_id=experiment_id, view_id=view_id):
            if view_id is not None:
                view = store.get_view(BasicExperimentPage, view_id)
                if view is not None and view.experiment_id == experiment_id:
                    return cast("BasicExperimentPage", view)
            return cast(
                "BasicExperimentPage",
                store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id),
            )
        case ComparisonRef(page_id=page_id):
            comparison = store.get_view(BasicExperimentPage, page_id)
            if comparison is None or comparison.project_id is None:
                msg = f"Comparison {page_id} is gone, or is not yours to see"
                raise LookupError(msg)
            return cast("BasicExperimentPage", comparison)


def ref_of(page: BasicExperimentPage) -> PageRef:
    """
    The `PageRef` that an already-loaded `page` itself represents.

    An experiment's shared page if it's nobody's, otherwise its own view id; or the comparison it is. A callback that
    reacts to `STATE_PAGE_STORAGE` (rather than to some other trigger that only
    incidentally carries `page`) should build its `PageRef` this way instead of from `STATE_VIEW_ID`:
    both changes land in the *same* server response as a structural edit that just branched into a
    new view, but `STATE_PAGE_STORAGE` is one of that response's own outputs, while `STATE_VIEW_ID`
    is only set by a *separate*, slightly later round trip (`_views.py`'s `sync_view_after_edit`) --
    reading it back too early would still see the view you were on before the edit branched.
    """
    match page.experiment_id, page.project_id:
        case int() as experiment_id, _:
            return ExperimentRef(experiment_id, page.id if page.owner_id is not None else None)
        case None, int():
            if page.owner_id is None:
                msg = f"Page {page.id} is a shared page of a project, and a project's pages are comparisons"
                raise ValueError(msg)
            return ComparisonRef(page.id)
        case _:
            msg = f"Page {page.id} is scoped to neither an experiment nor a project"
            raise ValueError(msg)


_FORK_NAME: typing.Final = "My view"
"""
What a view an edit branched into is called (numbered if you already have one). Named for whose it
is, not "Copy of ...": those stacked up as "Copy of Copy of Shared view" and said nothing.
"""

_FORK_COMPARISON_NAME: typing.Final = "My comparison"
"""What a comparison an edit branched into is called, likewise."""


def _unused_name(base: str, taken: Collection[str]) -> str:
    """The first of `base`, `"{base} 2"`, `"{base} 3"`, ... not in `taken`."""
    candidates = (base if n == 1 else f"{base} {n}" for n in itertools.count(1))
    return next(name for name in candidates if name not in taken)


def unique_view_name(store: DataStore[...], experiment_id: int, viewer_id: int) -> str:
    """
    The first of "My view", "My view 2", ... not already taken in `viewer_id`'s own view picker.

    "Taken" means any name already shown there -- `viewer_id`'s own views and anyone else's shared
    ones alike (`list_views`) -- so a freshly forked view never collides with (or looks like a
    rename of) one already in the same dropdown.
    """
    return _unused_name(_FORK_NAME, {view.name for view in store.list_views(experiment_id, viewer_id)})


def unique_comparison_name(store: DataStore[...], project_id: int, viewer_id: int) -> str:
    """The first of "My comparison", "My comparison 2", ... not already listed to `viewer_id` in the project (`list_comparisons`)."""
    return _unused_name(
        _FORK_COMPARISON_NAME, {c.name for c in store.list_comparisons(project_id, viewer_id)}
    )


def save_page(store: DataStore[...], mutated: BasicExperimentPage) -> BasicExperimentPage:
    """
    Persist `mutated`, branching into a new page of your own first if you don't already own it.

    Written in place if you already own the page it came from, otherwise created as a brand-new
    view of your own, seeded with `mutated`'s own (already-edited) content -- a copy of a comparison keeps its runs.
    "Owning" it means its `owner_id` is your own user id; the shared page (`owner_id` is `None`) and a view someone
    else owns both fail that check. This is what lets an edit made while looking at the shared page, or a
    view that isn't yours, branch into a view of your own the moment you make it -- no separate
    "save as a view" step first, and nobody else's page is ever changed by an edit that isn't theirs.
    """
    user_id = get_current_user().id
    if mutated.owner_id == user_id:
        return cast("BasicExperimentPage", store.update_page(mutated))
    match ref_of(mutated):
        case ExperimentRef(experiment_id=experiment_id):
            return cast(
                "BasicExperimentPage",
                store.create_view(
                    BasicExperimentPage,
                    models.NewPage[Any, Any](
                        experiment_id=experiment_id,
                        owner_id=user_id,
                        name=unique_view_name(store, experiment_id, user_id),
                        panels=mutated.panels,
                        page_settings=mutated.page_settings,
                    ),
                ),
            )
        case ComparisonRef():
            return copy_comparison(store, mutated)


def copy_comparison(store: DataStore[...], comparison: BasicExperimentPage) -> BasicExperimentPage:
    """A comparison of your own with `comparison`'s panels, settings and runs: what an edit that is not yours branches into."""
    project_id = cast("int", comparison.project_id)
    user_id = get_current_user().id
    return cast(
        "BasicExperimentPage",
        store.create_comparison(
            BasicExperimentPage,
            models.NewPage[Any, Any](
                project_id=project_id,
                owner_id=user_id,
                name=unique_comparison_name(store, project_id, user_id),
                panels=comparison.panels,
                page_settings=comparison.page_settings,
            ),
            frozenset(run.id for run in store.comparison_runs(comparison.id)),
        ),
    )


def save_document_settings(
    store: DataStore[...], ref: PageRef, change: DocumentSettings
) -> BasicExperimentPage:
    """Write `change` into `ref`'s page, branching into a view of your own first if it isn't yours (`save_page`)."""
    return save_page(store, load_page(store, ref).with_settings(change))
