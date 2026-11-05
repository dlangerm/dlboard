"""Retention's rules: how they read, how a change waits through its grace period, and who may set one past the caps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pendulum
import pytest

from dlboard import models
from dlboard.models import ProjectRole, Scope
from dlboard.plugins.retention import _within_caps
from dlboard.plugins.retention._rules import (
    RetentionKey,
    RetentionKind,
    RetentionRule,
    RetentionSetting,
    RetentionUnit,
    RuleVersion,
    RunAge,
    RunCount,
    Unlimited,
    describe,
)
from dlboard.plugins.retention._section import rule_of
from dlboard.serve._backend._authorization import AuthorizingDataStore
from dlboard.serve._backend._setting_rules import SettingRule

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_NOW = pendulum.datetime(2026, 1, 1)


@pytest.mark.parametrize(
    ("rule", "words"),
    [
        (Unlimited(), "Keeps every run"),
        (RunCount(max_runs=20), "Keeps the newest 20 runs of each experiment"),
        (RunCount(max_runs=5, unit=RetentionUnit.PROJECT), "Keeps the newest 5 runs of the project"),
        (RunAge(max_age_days=1), "Keeps runs from the last 1 day"),
        (RunAge(max_age_days=30), "Keeps runs from the last 30 days"),
    ],
)
def test_a_rule_is_described_in_words(rule: RetentionRule, words: str) -> None:
    assert describe(rule) == words


@pytest.mark.parametrize(
    ("kind", "amount", "unit", "rule"),
    [
        (RetentionKind.UNLIMITED, None, RetentionUnit.EXPERIMENT, Unlimited()),
        (RetentionKind.RUN_COUNT, 7, RetentionUnit.PROJECT, RunCount(max_runs=7, unit=RetentionUnit.PROJECT)),
        (RetentionKind.RUN_AGE, 9, RetentionUnit.PROJECT, RunAge(max_age_days=9)),
        (RetentionKind.RUN_COUNT, None, RetentionUnit.EXPERIMENT, "Say how many (1 or more)"),
        (RetentionKind.RUN_AGE, 0, RetentionUnit.EXPERIMENT, "Say how many (1 or more)"),
    ],
)
def test_the_form_says_a_rule_or_why_it_says_none(
    kind: RetentionKind, amount: int | None, unit: RetentionUnit, rule: RetentionRule | str
) -> None:
    assert rule_of(kind, amount, unit) == rule


def test_a_changed_rule_waits_pending_while_the_rule_before_it_is_in_effect_and_saving_again_never_delays_it() -> (
    None
):
    grace = pendulum.duration(days=7)
    first = RetentionSetting().saved(RunCount(max_runs=50), by=1, now=_NOW, grace=grace)
    assert first.in_effect(_NOW) is None
    assert first.in_effect(_NOW.add(days=7)) == RunCount(max_runs=50)

    second = first.as_of(_NOW.add(days=7)).saved(
        RunCount(max_runs=10), by=1, now=_NOW.add(days=8), grace=grace
    )
    third = second.saved(RunCount(max_runs=5), by=1, now=_NOW.add(days=12), grace=grace)
    assert second.in_effect(_NOW.add(days=9)) == RunCount(max_runs=50)
    assert third.pending is not None
    assert third.pending.effective_at == _NOW.add(days=19)
    assert third.in_effect(_NOW.add(days=13)) == RunCount(max_runs=50)
    assert third.saved(RunCount(max_runs=50), by=1, now=_NOW.add(days=13), grace=grace).pending is None


def _as(store: SQLLiteStore, name: str, *scopes: Scope) -> tuple[AuthorizingDataStore, models.User]:
    user = store.get_or_create_user(models.Principal(issuer="test", subject=name, username=name))
    user = store.update_user(user.model_copy(update={"scopes": list(scopes)}))
    rules = {str(RetentionKey.RULE): SettingRule(key=RetentionKey.RULE, check=_within_caps)}
    return AuthorizingDataStore(store, user, enforce_grants=True, setting_rules=rules), user


def _saved(rule: RetentionRule, by: int) -> dict[str, Any]:
    return RetentionSetting().saved(rule, by=by, now=_NOW, grace=pendulum.duration()).dump()


def test_an_owner_stays_within_the_caps_and_an_admin_may_pass_them_and_the_rule_says_so(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_MAX_RUNS", "10")
    (as_owner, owner), (as_admin, admin) = _as(store, "owner"), _as(store, "admin", Scope.ALL)
    project = as_owner.create_project(models.NewProject(name="p", description="")).id
    store.set_project_grant(
        models.NewProjectGrant(project_id=project, user_id=owner.id, role=ProjectRole.OWNER)
    )

    as_owner.put_scoped_setting(
        RetentionKey.RULE, project, None, _saved(RunCount(max_runs=10), owner.id), owner
    )
    for past in (RunCount(max_runs=11), Unlimited(), RunAge(max_age_days=5)):
        with pytest.raises(PermissionError, match="cap"):
            as_owner.put_scoped_setting(RetentionKey.RULE, project, None, _saved(past, owner.id), owner)

    as_admin.put_scoped_setting(
        RetentionKey.RULE, project, None, _saved(RunCount(max_runs=11), admin.id), admin
    )
    stored = store.get_scoped_settings(RetentionKey.RULE, project).project
    assert stored is not None
    pending = RetentionSetting.model_validate(stored.value).pending
    assert pending is not None
    assert (pending.rule, pending.cap_exempt) == (RunCount(max_runs=11), True)


def test_an_owner_cannot_change_the_rule_in_effect_or_mark_a_rule_exempt_from_the_caps(
    store: SQLLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLBOARD_RETENTION_MAX_RUNS", "10")
    as_owner, owner = _as(store, "owner")
    project = as_owner.create_project(models.NewProject(name="p", description="")).id
    store.set_project_grant(
        models.NewProjectGrant(project_id=project, user_id=owner.id, role=ProjectRole.OWNER)
    )
    now = pendulum.now("UTC")
    in_effect = RuleVersion(rule=RunCount(max_runs=5), set_at=now, effective_at=now)

    def write(setting: RetentionSetting) -> None:
        as_owner.put_scoped_setting(RetentionKey.RULE, project, None, setting.dump(), owner)

    with pytest.raises(PermissionError, match="server's to change"):
        write(RetentionSetting(active=in_effect))  # a rule nobody saved through its grace period
    store.put_scoped_setting(
        RetentionKey.RULE, project, None, RetentionSetting(active=in_effect).dump(), owner
    )
    with pytest.raises(PermissionError, match="server's to change"):
        write(RetentionSetting(active=in_effect.model_copy(update={"rule": RunCount(max_runs=9)})))

    pending = RuleVersion(rule=RunCount(max_runs=6), set_at=now, effective_at=now, cap_exempt=True)
    write(RetentionSetting(active=in_effect, pending=pending))  # within the cap, whatever it says of itself
    stored = store.get_scoped_settings(RetentionKey.RULE, project).project
    assert stored is not None
    kept = RetentionSetting.model_validate(stored.value).pending
    assert kept is not None
    assert (kept.rule, kept.cap_exempt) == (RunCount(max_runs=6), False)
