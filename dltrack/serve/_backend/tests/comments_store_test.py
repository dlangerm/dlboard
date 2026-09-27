from __future__ import annotations

from typing import TYPE_CHECKING

from dltrack import models

if TYPE_CHECKING:
    from dltrack.plugins.data_stores.sqlite import SQLLiteStore


def _notes_revision(store: SQLLiteStore, experiment_id: int) -> int:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    return experiment.notes_revision


def test_a_thread_lists_notes_oldest_first_and_bumps_only_the_notes_revision(
    store: SQLLiteStore, experiment_id: int
) -> None:
    me = store.get_or_create_user("me")
    revision_before = store.get_experiment(experiment_id).revision  # pyright: ignore[reportOptionalMemberAccess]

    first = store.add_comment(
        models.NewComment(experiment_id=experiment_id, author_id=me.id, body="lr too high?")
    )
    second = store.add_comment(
        models.NewComment(
            experiment_id=experiment_id, author_id=me.id, body="yes", run_ids=[3], mentioned_user_ids=[me.id]
        )
    )

    assert [c.id for c in store.list_comments(experiment_id)] == [first.id, second.id]
    assert store.list_comments(experiment_id)[1].run_ids == [3]
    assert _notes_revision(store, experiment_id) == 2
    # Posting a note mustn't look like new data to the chart poll.
    assert store.get_experiment(experiment_id).revision == revision_before  # pyright: ignore[reportOptionalMemberAccess]


def test_only_its_author_can_delete_a_note(store: SQLLiteStore, experiment_id: int) -> None:
    me, them = store.get_or_create_user("me"), store.get_or_create_user("them")
    note = store.add_comment(models.NewComment(experiment_id=experiment_id, author_id=me.id, body="mine"))

    store.delete_comment(note.id, them.id)
    assert [c.id for c in store.list_comments(experiment_id)] == [note.id]

    store.delete_comment(note.id, me.id)
    assert store.list_comments(experiment_id) == []
    assert _notes_revision(store, experiment_id) == 2


def test_list_users_is_sorted_by_username(store: SQLLiteStore) -> None:
    for name in ("carol", "alice", "bob"):
        store.get_or_create_user(name)

    assert [u.username for u in store.list_users()] == ["alice", "bob", "carol"]
