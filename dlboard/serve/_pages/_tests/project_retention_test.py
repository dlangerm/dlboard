"""The Retention section's words for a rule, and the rule its form says."""

from __future__ import annotations

import pytest

from dlboard import models
from dlboard.models import RetentionKind, RetentionUnit
from dlboard.serve._pages._project_retention import describe, rule_of


@pytest.mark.parametrize(
    ("rule", "words"),
    [
        (models.Unlimited(), "Keeps every run"),
        (models.RunCount(max_runs=20), "Keeps the newest 20 runs of each experiment"),
        (models.RunCount(max_runs=5, unit=RetentionUnit.PROJECT), "Keeps the newest 5 runs of the project"),
        (models.RunAge(max_age_days=1), "Keeps runs from the last 1 day"),
        (models.RunAge(max_age_days=30), "Keeps runs from the last 30 days"),
    ],
)
def test_a_rule_is_described_in_words(rule: models.RetentionRule, words: str) -> None:
    assert describe(rule) == words


@pytest.mark.parametrize(
    ("kind", "amount", "unit", "rule"),
    [
        (RetentionKind.UNLIMITED, None, RetentionUnit.EXPERIMENT, models.Unlimited()),
        (
            RetentionKind.RUN_COUNT,
            7,
            RetentionUnit.PROJECT,
            models.RunCount(max_runs=7, unit=RetentionUnit.PROJECT),
        ),
        (RetentionKind.RUN_AGE, 9, RetentionUnit.PROJECT, models.RunAge(max_age_days=9)),
        (RetentionKind.RUN_COUNT, None, RetentionUnit.EXPERIMENT, "Say how many (1 or more)"),
        (RetentionKind.RUN_AGE, 0, RetentionUnit.EXPERIMENT, "Say how many (1 or more)"),
    ],
)
def test_the_form_says_a_rule_or_why_it_says_none(
    kind: RetentionKind, amount: int | None, unit: RetentionUnit, rule: models.RetentionRule | str
) -> None:
    assert rule_of(kind, amount, unit) == rule
