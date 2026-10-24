"""`*` patterns: which names each one matches, a `*` standing for any run of characters."""

from __future__ import annotations

import pytest

from dlboard.models import KeyPatterns
from dlboard.models._key_patterns import matches


@pytest.mark.parametrize(
    ("pattern", "key", "matched"),
    [
        pytest.param("loss", "loss", True, id="a-name-matches-itself"),
        pytest.param("loss", "losses", False, id="a-name-matches-nothing-longer"),
        pytest.param("*", "", True, id="every-key-even-an-empty-one"),
        pytest.param("*", "a/b", True, id="every-key-with-a-slash"),
        pytest.param("eval/*", "eval/a", True, id="a-prefix"),
        pytest.param("eval/*", "evalx", False, id="a-prefix-ends-at-its-slash"),
        pytest.param("layer_*/grad_norm", "layer_12/grad_norm", True, id="a-star-in-the-middle"),
        pytest.param("layer_*/grad_norm", "layer_12/x/grad_norm", True, id="a-star-spans-slashes"),
        pytest.param("layer_*/grad_norm", "layer_grad_norm", False, id="the-suffix-must-be-there"),
        pytest.param("layer_*/grad_norm", "layer_1/grad_norm_x", False, id="nothing-after-the-suffix"),
        pytest.param("a*b*c", "axbxc", True, id="stars-in-order"),
        pytest.param("a*b*c", "acb", False, id="out-of-order-does-not-match"),
        pytest.param("a**b", "ab", True, id="adjacent-stars"),
        pytest.param("*a*", "xyz", False, id="the-middle-part-must-appear"),
        pytest.param("ab*ab", "aba", False, id="parts-may-not-overlap"),
        pytest.param("ab*ab", "abab", True, id="parts-may-meet"),
        pytest.param("a*", "A", False, id="case-counts"),
        pytest.param("a[?]*", "a?x", False, id="brackets-and-question-marks-are-plain"),
    ],
)
def test_a_pattern_matches_the_keys_its_stars_stand_for(pattern: str, key: str, matched: bool) -> None:
    assert matches(pattern, key) is matched


def test_a_list_of_patterns_matches_a_key_any_of_them_matches() -> None:
    asked = KeyPatterns(("loss", "layer_*/grad_norm"))

    assert asked.matches("loss")
    assert asked.matches("layer_1/grad_norm")
    assert not asked.matches("layer_1/acc")
    assert asked.exact == {"loss"}
    assert asked.wildcards == {"layer_*/grad_norm"}
    assert not asked.everything
    assert KeyPatterns(("*",)).everything
