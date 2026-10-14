"""
Where an experiment page is read from and written to: the shared page, or a named view of it.

The one place a page is saved. An edit to a page you don't own -- the shared page, or someone else's view --
branches into a view of your own first (`save_page`), so nobody else's page ever changes under an edit that
isn't theirs.
"""

from __future__ import annotations

import itertools
import typing
from typing import TYPE_CHECKING, Any, cast

from dlboard import models
from dlboard.serve import get_current_user
from dlboard.serve._pages._experiment._page_model import BasicExperimentPage

if TYPE_CHECKING:
    from dlboard.models import DataStore


class PageRef(typing.NamedTuple):
    """
    Which page an experiment-page callback reads and writes: the shared one, or a named view of it.

    Every callback that loads the page by experiment builds one of these from `STATE_EXPERIMENT_ID`
    and `STATE_VIEW_ID`, so an edit made while looking at a view lands in that view, never the
    shared page. (Callbacks that already hold the page itself -- `STATE_PAGE_STORAGE` -- write it back
    by its own id, which is right either way.)
    """

    experiment_id: int
    view_id: int | None


def load_page(store: DataStore[...], ref: PageRef) -> BasicExperimentPage:
    """The page `ref` points at; the shared page if its view no longer exists or isn't this experiment's."""
    if ref.view_id is not None:
        view = store.get_view(BasicExperimentPage, ref.view_id)
        if view is not None and view.experiment_id == ref.experiment_id:
            return cast("BasicExperimentPage", view)
    return cast(
        "BasicExperimentPage", store.get_or_create_page(BasicExperimentPage, experiment_id=ref.experiment_id)
    )


def ref_of(experiment_id: int, page: BasicExperimentPage) -> PageRef:
    """
    The `PageRef` that an already-loaded `page` itself represents.

    The shared page if it's nobody's, otherwise its own view id. A callback that reacts to
    `STATE_PAGE_STORAGE` (rather than to some other trigger that only
    incidentally carries `page`) should build its `PageRef` this way instead of from `STATE_VIEW_ID`:
    both changes land in the *same* server response as a structural edit that just branched into a
    new view, but `STATE_PAGE_STORAGE` is one of that response's own outputs, while `STATE_VIEW_ID`
    is only set by a *separate*, slightly later round trip (`_views.py`'s `sync_view_after_edit`) --
    reading it back too early would still see the view you were on before the edit branched.
    """
    return PageRef(experiment_id, page.id if page.owner_id is not None else None)


_FORK_NAME: typing.Final = "My view"
"""
What a view an edit branched into is called (numbered if you already have one). Named for whose it
is, not "Copy of ...": those stacked up as "Copy of Copy of Shared view" and said nothing.
"""


def unique_view_name(store: DataStore[...], experiment_id: int, viewer_id: int) -> str:
    """
    The first of "My view", "My view 2", ... not already taken in `viewer_id`'s own view picker.

    "Taken" means any name already shown there -- `viewer_id`'s own views and anyone else's shared
    ones alike (`list_views`) -- so a freshly forked view never collides with (or looks like a
    rename of) one already in the same dropdown.
    """
    taken = {view.name for view in store.list_views(experiment_id, viewer_id)}
    candidates = (_FORK_NAME if n == 1 else f"{_FORK_NAME} {n}" for n in itertools.count(1))
    return next(name for name in candidates if name not in taken)


def save_page(store: DataStore[...], mutated: BasicExperimentPage) -> BasicExperimentPage:
    """
    Persist `mutated`, branching into a new view of your own first if you don't already own it.

    Written in place if you already own the page it came from, otherwise created as a brand-new
    view of your own, seeded with `mutated`'s own (already-edited) content. "Owning" it means its
    `owner_id` is your own user id; the shared page (`owner_id` is `None`) and a view someone else
    owns both fail that check. This is what lets an edit made while looking at the shared page, or a
    view that isn't yours, branch into a view of your own the moment you make it -- no separate
    "save as a view" step first, and nobody else's page is ever changed by an edit that isn't theirs.
    """
    user_id = get_current_user().id
    if mutated.owner_id == user_id:
        return cast("BasicExperimentPage", store.update_page(mutated))
    experiment_id = cast("int", mutated.experiment_id)
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


def persist_settings(
    store: DataStore[...], ref: PageRef, updates: dict[str, Any], *, branch_on_edit: bool = True
) -> BasicExperimentPage:
    """
    Merge `updates` into page_settings (server-authoritative) and persist -- no accordion rebuild.

    `branch_on_edit` (on by default) branches into a view of your own first if `ref` doesn't already
    point at one you own -- see `save_page`. The two call sites that pass `False` (which panel/tab
    is open) are purely per-viewer browsing state: opening a panel isn't "editing the view" the way
    deleting a chart or excluding a run is, so those two keep writing straight to whatever page
    `ref` names, exactly as before this existed.
    """
    page = load_page(store, ref)
    new_settings = {**page.page_settings, **updates}
    mutated = page.model_copy(update={"page_settings": new_settings})
    if not branch_on_edit:
        return cast("BasicExperimentPage", store.update_page(mutated))
    return save_page(store, mutated)
