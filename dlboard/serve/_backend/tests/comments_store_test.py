from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard import models
from dlboard.serve._backend import _sql_store_base as store_base

if TYPE_CHECKING:
    import pytest

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def _notes_revision(store: SQLLiteStore, experiment_id: int) -> int:
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    return experiment.notes_revision


def test_a_thread_lists_notes_oldest_first_and_bumps_only_the_notes_revision(
    store: SQLLiteStore, experiment_id: int
) -> None:
    me = store.get_or_create_user(models.Principal.unverified("me"))
    revision_before = store.get_experiment(experiment_id).revision  # pyrefly: ignore [missing-attribute]

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
    assert (
        store.get_experiment(experiment_id).revision == revision_before  # pyrefly: ignore [missing-attribute]
    )


def test_only_its_author_can_delete_a_note(store: SQLLiteStore, experiment_id: int) -> None:
    me, them = (
        store.get_or_create_user(models.Principal.unverified("me")),
        store.get_or_create_user(models.Principal.unverified("them")),
    )
    note = store.add_comment(models.NewComment(experiment_id=experiment_id, author_id=me.id, body="mine"))

    store.delete_comment(note.id, them.id)
    assert [c.id for c in store.list_comments(experiment_id)] == [note.id]

    store.delete_comment(note.id, me.id)
    assert store.list_comments(experiment_id) == []
    assert _notes_revision(store, experiment_id) == 2


def test_get_users_is_sorted_by_username(store: SQLLiteStore) -> None:
    for name in ("carol", "alice", "bob"):
        store.get_or_create_user(models.Principal.unverified(name))

    assert [u.username for u in store.get_users()] == ["alice", "bob", "carol"]


def test_get_users_lists_people_and_finds_dlboards_own_users_only_by_id(store: SQLLiteStore) -> None:
    """Retention and the Trash act as users of their own, which are never offered as people (#647)."""
    alice = store.get_or_create_user(models.Principal.unverified("alice"))
    retention = store.get_or_create_user(models.Principal.system("retention"))

    assert [u.username for u in store.get_users()] == ["alice"]
    assert store.get_users(name_contains="dlboard") == []
    assert [u.username for u in store.get_users({alice.id, retention.id})] == ["alice", "dlboard:retention"]


def test_get_users_asked_for_by_more_ids_than_a_statement_binds_is_still_one_sorted_and_limited_list(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_base, "_IN_BATCH", 2)
    ids = {n: store.get_or_create_user(models.Principal.unverified(n)).id for n in "edcba"}

    found = store.get_users(set(ids.values()), limit=4)

    assert [u.username for u in found] == ["a", "b", "c", "d"]


def test_get_users_is_narrowed_by_id_by_name_whatever_its_case_and_by_limit(store: SQLLiteStore) -> None:
    ids = {
        name: store.get_or_create_user(models.Principal.unverified(name)).id
        for name in ("carol", "alice", "bob")
    }

    def names(
        user_ids: set[int] | None = None, *, name_contains: str = "", limit: int | None = None
    ) -> list[str]:
        return [u.username for u in store.get_users(user_ids, name_contains=name_contains, limit=limit)]

    # Only who exists, whoever it is asked for; no one asked for is no one, not everyone.
    assert names({ids["bob"], ids["carol"], 999}) == ["bob", "carol"]
    assert names(set()) == []
    assert names(name_contains="AR") == ["carol"]
    assert names({ids["alice"], ids["bob"]}, name_contains="b") == ["bob"]
    assert names(limit=2) == ["alice", "bob"]
    # A name is searched for as written, not as a pattern.
    assert names(name_contains="%") == []
