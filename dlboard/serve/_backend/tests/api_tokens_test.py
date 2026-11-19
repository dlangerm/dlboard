"""Tests for `dlboard.serve._backend._api_tokens`: minting and verifying API tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pendulum
import pytest

from dlboard import models
from dlboard.serve._backend import _api_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

    from dlboard.plugins.data_stores.sqlite import SQLLiteStore


def test_a_minted_token_authenticates_as_its_owner_and_is_stored_hashed(store: SQLLiteStore) -> None:
    owner = store.get_or_create_user(models.Principal.unverified("owner"))

    token, raw = _api_tokens.mint_api_token(store, owner, "ci")

    assert raw.split("_", 2)[2] not in token.secret_hash
    bearer = _api_tokens.user_for_token(store, raw)
    assert bearer is not None
    assert (bearer.user.id, bearer.run_id) == (owner.id, None)
    (listed,) = store.list_api_tokens(owner.id)
    assert listed.last_used_at is not None


def _revoke(store: SQLLiteStore, token: models.ApiToken, owner: models.User) -> None:
    store.revoke_api_token(token.id, owner.id)


def _disable(store: SQLLiteStore, _token: models.ApiToken, owner: models.User) -> None:
    store.update_user(owner.model_copy(update={"disabled_at": pendulum.now(pendulum.UTC)}))


@pytest.mark.parametrize("invalidate", [_revoke, _disable])
def test_a_revoked_token_or_disabled_owner_stops_authenticating(
    store: SQLLiteStore, invalidate: Callable[[SQLLiteStore, models.ApiToken, models.User], None]
) -> None:
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    token, raw = _api_tokens.mint_api_token(store, owner, "ci")

    invalidate(store, token, owner)

    assert _api_tokens.user_for_token(store, raw) is None


def test_an_expired_token_stops_authenticating(store: SQLLiteStore) -> None:
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    _, raw = _api_tokens.mint_api_token(
        store, owner, "ci", expires_at=pendulum.now(pendulum.UTC).subtract(seconds=1)
    )

    assert _api_tokens.user_for_token(store, raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "dlb_nope_nope",
        "dlb_",
        "Bearer something-else",
        "{token}\n",
        "{token}extra",
        "{token}_more",
        "x{token}",
    ],
)
def test_only_a_whole_well_formed_token_authenticates(store: SQLLiteStore, raw: str) -> None:
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    _, minted = _api_tokens.mint_api_token(store, owner, "ci")
    candidate = raw.format(token=minted)

    assert _api_tokens.user_for_token(store, candidate) is None


def _flip_last_char(raw: str) -> str:
    return raw[:-1] + ("A" if raw[-1] != "A" else "B")


def _unknown_token(_raw: str) -> str:
    return "dlb_" + "0" * 16 + "_" + "A" * 43


@pytest.mark.parametrize("tamper", [_flip_last_char, _unknown_token])
def test_a_tampered_or_unknown_token_is_rejected(store: SQLLiteStore, tamper: Callable[[str], str]) -> None:
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    _, raw = _api_tokens.mint_api_token(store, owner, "ci")

    assert _api_tokens.user_for_token(store, tamper(raw)) is None


def test_a_run_token_is_bound_to_its_run_expires_and_is_not_listed_with_the_owners_tokens(
    store: SQLLiteStore, experiment_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RUN_TOKEN_LIFETIME_HOURS", "2")
    owner = store.get_or_create_user(models.Principal.unverified("owner"))
    run = store.create_run(models.NewRun(experiment_id=experiment_id))

    token, raw = _api_tokens.mint_run_token(store, owner, run.id)

    bearer = _api_tokens.user_for_token(store, raw)
    assert bearer is not None
    assert (bearer.user.id, bearer.run_id, raw[:4]) == (owner.id, run.id, "dlr_")
    assert token.expires_at is not None
    assert pendulum.duration(hours=2) - (token.expires_at - token.created_at) < pendulum.duration(seconds=5)
    assert store.list_api_tokens(owner.id) == []
    # The same secret with a full token's prefix is not a full token: a token's prefix must say what it is.
    assert _api_tokens.user_for_token(store, "dlb_" + raw[4:]) is None
