"""Patterns: which names each one matches, a `*` standing for any run of characters and a `#` for a run of digits."""

from __future__ import annotations

import re
from itertools import chain

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlboard.models import KeyPatterns
from dlboard.models._key_patterns import captures, family_template, is_pattern, literal_prefix, matches

# Few characters, so a pattern and a key drawn apart often nearly match. Each is a rule: a `*` spans slashes and lines,
# case counts, `.` and `[` are plain text, and a digit need not be ASCII.
_TEXT = st.text(alphabet="aA1٣/.[\n", max_size=4)
_DIGITS = st.text(alphabet="1٣", min_size=1, max_size=3)
_PARTS = st.lists(st.one_of(_TEXT, st.sampled_from("*#")), max_size=5)
_WILDCARD = re.compile(r"\*+|#")


@st.composite
def _pattern_and_a_key_it_names(draw: st.DrawFn) -> tuple[str, str]:
    """A pattern, and a key made from it by putting text in for each `*` and digits in for each `#`."""
    parts = draw(_PARTS)
    stand_ins = [draw(_TEXT) if part == "*" else draw(_DIGITS) if part == "#" else part for part in parts]
    return "".join(parts), "".join(stand_ins)


def _filled(pattern: str, got: tuple[str, ...]) -> str:
    """`pattern` with each wildcard (adjacent `*`s being one) replaced by what `got` says it stood for."""
    return "".join(chain.from_iterable(zip(_WILDCARD.split(pattern), (*got, ""), strict=True)))


@given(_pattern_and_a_key_it_names())
def test_a_pattern_names_every_key_its_wildcards_can_stand_for(pattern_and_key: tuple[str, str]) -> None:
    pattern, key = pattern_and_key

    got = captures(pattern, key)

    assert got is not None
    assert key == pattern or _filled(pattern, got) == key


@given(_PARTS.map("".join), _TEXT)
def test_a_pattern_names_no_other_key_than_those_and_itself(pattern: str, key: str) -> None:
    got = captures(pattern, key)

    if got is not None and key != pattern:
        assert _filled(pattern, got) == key
        wildcards = _WILDCARD.findall(pattern)
        assert all(text.isdecimal() for text, wildcard in zip(got, wildcards, strict=True) if wildcard == "#")


def test_a_list_of_patterns_matches_a_key_any_of_them_matches() -> None:
    asked = KeyPatterns(("loss", "layer_*/grad_norm"))

    assert asked.matches("loss")
    assert asked.matches("layer_1/grad_norm")
    assert not asked.matches("layer_1/acc")
    assert asked.exact == {"loss"}
    assert asked.wildcards == {"layer_*/grad_norm"}
    assert not asked.everything
    assert KeyPatterns(("*",)).everything


@pytest.mark.parametrize(
    ("pattern", "key", "got"),
    [
        pytest.param("accuracy/#,#/*", "accuracy/3,7/dog", ("3", "7", "dog"), id="each-wildcard-in-order"),
        pytest.param("loss", "loss", (), id="a-name-has-none"),
        pytest.param("*/*", "a/b/c", ("a", "b/c"), id="stars-take-as-little-as-they-can"),
        pytest.param("a**b", "axb", ("x",), id="adjacent-stars-are-one"),
        pytest.param("accuracy/#,#/*", "loss", None, id="no-match-is-none"),
    ],
)
def test_the_captures_are_what_each_wildcard_stood_for(
    pattern: str, key: str, got: tuple[str, ...] | None
) -> None:
    assert captures(pattern, key) == got


def test_the_first_pattern_that_names_a_key_gives_its_captures() -> None:
    asked = KeyPatterns(("loss/#", "accuracy/#,#/*"))

    assert asked.capture("accuracy/3,7/dog") == ("3", "7", "dog")
    assert asked.capture("loss/4") == ("4",)
    assert asked.capture("acc") is None


@given(_PARTS.map("".join))
def test_a_pattern_starts_with_the_text_before_its_first_wildcard(pattern: str) -> None:
    prefix = literal_prefix(pattern)

    assert pattern.startswith(prefix)
    assert not is_pattern(prefix)
    assert pattern.removeprefix(prefix)[:1] in {"", "*", "#"}


@given(_PARTS.map("".join))
def test_a_name_with_a_wildcard_in_it_still_names_itself(name: str) -> None:
    assert matches(name, name)


_FAMILY_KEYS = st.text(alphabet="a1٣/", max_size=5)


@given(_FAMILY_KEYS, _FAMILY_KEYS)
def test_a_family_template_names_the_keys_of_its_family_and_no_others(key: str, other: str) -> None:
    """Each run of digits becomes a `#`, so `layer_3/grad_norm` and `layer_17/grad_norm` share `layer_#/grad_norm`."""
    template = family_template(key)

    assert not any(char.isdecimal() for char in template)
    assert matches(template, key)
    assert matches(template, other) is (family_template(other) == template)
    assert family_template(f"{key}*") == f"{key}*"  # a key with a wildcard in it is its own family
