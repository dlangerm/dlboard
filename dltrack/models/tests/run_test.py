"""Tests for `NewRun`'s auto-generated name default."""

from __future__ import annotations

import re

from dltrack.models._run import NewRun

_SLUG_SHAPE = re.compile(r"[a-z]+-[a-z]+")
"""`coolname.generate_slug(2)`'s actual shape: two lowercase words joined by one hyphen, no digits."""


def test_an_omitted_name_is_auto_generated() -> None:
    assert _SLUG_SHAPE.fullmatch(NewRun(experiment_id=1).name or "")


def test_two_omitted_names_are_not_the_same() -> None:
    """Proves the factory is actually invoked per-instance, not baked in as a frozen default."""
    names = {NewRun(experiment_id=1).name for _ in range(10)}
    assert len(names) > 1


def test_an_explicit_none_name_is_left_alone() -> None:
    """
    A `default_factory` only fires when the field is *omitted*, never when it's explicitly `None`.

    This is the exact distinction `SQLStoreBase._fetch` relies on to read a pre-existing,
    genuinely-`NULL` run back as `name=None` rather than retroactively generating one -- a SQL
    row's mapping always has the `"name"` key present, even when its value is `NULL`.
    """
    assert NewRun(experiment_id=1, name=None).name is None


def test_an_explicit_name_is_kept_as_given() -> None:
    assert NewRun(experiment_id=1, name="custom").name == "custom"
