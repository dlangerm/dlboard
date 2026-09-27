from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dltrack import models
from dltrack.models import NewPage, PanelInstance
from dltrack.serve._pages._experiment._experiment_page_state import BasicExperimentPage

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def _view(store: SQLLiteStore, experiment_id: int, owner: models.User, name: str) -> int:
    view = store.create_view(
        BasicExperimentPage,
        NewPage[Any, Any](
            experiment_id=experiment_id,
            owner_id=owner.id,
            name=name,
            panels=[PanelInstance[Any, Any](name=name)],
        ),
    )
    return view.id


def test_views_never_stand_in_for_the_shared_page(store: SQLLiteStore, experiment_id: int) -> None:
    owner = store.get_or_create_user("me")
    view_id = _view(store, experiment_id, owner, "mine")

    shared = store.get_or_create_page(BasicExperimentPage, experiment_id=experiment_id)

    assert shared.id != view_id
    assert shared.owner_id is None
    assert shared.panels == []
    assert store.get_view(BasicExperimentPage, shared.id) is None


def test_each_user_lists_and_deletes_only_their_own_views(store: SQLLiteStore, experiment_id: int) -> None:
    me, them = store.get_or_create_user("me"), store.get_or_create_user("them")
    b = _view(store, experiment_id, me, "b")
    a = _view(store, experiment_id, me, "a")
    theirs = _view(store, experiment_id, them, "theirs")

    assert store.list_views(experiment_id, me.id) == [
        models.ViewSummary(id=a, name="a"),
        models.ViewSummary(id=b, name="b"),
    ]
    store.delete_view(theirs, me.id)
    store.delete_view(a, me.id)

    assert store.get_view(BasicExperimentPage, theirs) is not None
    assert store.get_view(BasicExperimentPage, a) is None
    assert [v.id for v in store.list_views(experiment_id, me.id)] == [b]
