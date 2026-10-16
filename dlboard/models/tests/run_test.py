"""Tests for `NewRun`'s auto-generated name default."""

from __future__ import annotations

import random
import re
from typing import TYPE_CHECKING

import pytest

from dlboard.models._run import NewRun

if TYPE_CHECKING:
    from collections.abc import Iterator

_SLUG_SHAPE = re.compile(r"[a-z]+-[a-z]+")
"""`coolname.generate_slug(2)`'s actual shape: two lowercase words joined by one hyphen, no digits."""


@pytest.fixture
def seeded_global_random() -> Iterator[None]:
    """Seed the global `random` the way a training script does, then put its state back."""
    state = random.getstate()
    random.seed(0)
    yield
    random.setstate(state)


def test_an_omitted_name_is_auto_generated() -> None:
    assert _SLUG_SHAPE.fullmatch(NewRun(experiment_id=1).name or "")


def test_two_omitted_names_are_not_the_same() -> None:
    """Proves the factory is actually invoked per-instance, not baked in as a frozen default."""
    names = {NewRun(experiment_id=1).name for _ in range(10)}
    assert len(names) > 1


@pytest.mark.usefixtures("seeded_global_random")
def test_names_still_differ_under_a_seeded_global_random() -> None:
    """Each run starts right after its script seeds `random`, as `seed_everything` does, and still gets its own name."""
    names: set[str | None] = set()
    for _ in range(10):
        random.seed(0)
        names.add(NewRun(experiment_id=1).name)
    assert len(names) > 1


@pytest.mark.usefixtures("seeded_global_random")
def test_naming_a_run_leaves_the_global_random_stream_alone() -> None:
    expected = [random.random() for _ in range(3)]
    random.seed(0)
    NewRun(experiment_id=1)
    assert [random.random() for _ in range(3)] == expected


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
