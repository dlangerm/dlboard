"""The people a note can mention: a page of them by name, the rest found by search, and always those already picked (#501)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard import models
from dlboard.serve._pages._experiment._notes import MENTION_LIMIT, _mention_options

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_a_picker_lists_the_first_page_of_people_keeps_its_picks_and_searches_the_rest(
    store: SQLLiteStore,
) -> None:
    users = [
        store.get_or_create_user(models.Principal.unverified(f"user-{i:03d}"))
        for i in range(MENTION_LIMIT + 10)
    ]
    last = users[-1]

    listed = _mention_options(store)
    kept = _mention_options(store, keep=[str(last.id)])
    found = _mention_options(store, search="USER-059")

    assert [option["label"] for option in listed] == [user.username for user in users[:MENTION_LIMIT]]
    assert kept[0] == {"value": str(last.id), "label": "user-059"}
    assert len(kept) == MENTION_LIMIT + 1
    assert found == [{"value": str(last.id), "label": "user-059"}]
