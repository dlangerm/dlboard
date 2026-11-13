"""
Retention on a project's page: a line under its header with the rule it is held to, and a section to see and change it.

Each experiment's page has a line under its header too, with its own rule and its project's, linking to that section.

One form sets a rule for the project (an owner's) or for one of its experiments (any editor's, stricter than the
project's). A saved rule waits out the grace period, shown as pending with what it would delete, while the rule before it
is enforced. Saving or cancelling draws the section again in place: every callback here writes only inside the section,
so none is called as the page draws it (each such one is a round trip a page load waits on, `page_load_budget_test`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import dash_mantine_components as dmc
import pendulum
from dash import Dash, Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from dlboard.models import ProjectRole, Scope, has_scope
from dlboard.plugins.retention._candidates import Candidates, Rules
from dlboard.plugins.retention._rules import (
    RetentionKey,
    RetentionKind,
    RetentionRule,
    RetentionSetting,
    RetentionStatus,
    RetentionUnit,
    RunAge,
    RunCount,
    Unlimited,
    describe,
    keeps_at_most,
)
from dlboard.plugins.retention._settings import OVERDUE_COUNTED, RetentionSettings
from dlboard.serve import ClientsideScript, get_current_user, get_data_store, get_project_role, relative_path
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ValueId
from dlboard.serve._pages._dash_helpers import CallbackStates
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._experiment_picker import (
    experiment_options,
    experiment_search_store,
    register_search_gate,
    searched,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from dash.development.base_component import Component

    from dlboard.models import DataStore, Experiment, Project, ScopedSetting


class _RetentionSection:
    """Page tag: marks a component id as belonging to the project page's Retention section."""


SCOPE_ID: ValueId[_RetentionSection] = ValueId("retention-scope")
KIND_ID: ValueId[_RetentionSection] = ValueId("retention-kind")
AMOUNT_ID: ValueId[_RetentionSection] = ValueId("retention-amount")
UNIT_ID: ValueId[_RetentionSection] = ValueId("retention-unit")
SAVE_ID: ButtonId[_RetentionSection] = ButtonId("retention-save")
CANCEL_ID: ButtonId[_RetentionSection] = ButtonId("retention-cancel")
PREVIEW_BUTTON_ID: ButtonId[_RetentionSection] = ButtonId("retention-preview-button")
PREVIEW_ID: DivId[_RetentionSection] = DivId("retention-preview")
ERROR_ID: DivId[_RetentionSection] = DivId("retention-error")
SECTION_ID: DivId[_RetentionSection] = DivId("retention-section")
RETENTION_TAB: Final = "retention"
"""The project page's tab retention draws in: its `?tab=`, which the experiment page's line links to."""

PROJECT_SCOPE: Final = "project"
"""The scope select's value for the project's own rule; an experiment's is its id."""
PREVIEW_LIMIT: Final = 1_000
"""How many runs the preview counts to: past that it says only that many or more."""

_CLEAR_PREVIEW_JS = ClientsideScript(Path(__file__).with_name("clear_preview.js"))

_KINDS: Final = [
    {"value": RetentionKind.UNLIMITED.value, "label": "Every run"},
    {"value": RetentionKind.RUN_COUNT.value, "label": "The newest runs"},
    {"value": RetentionKind.RUN_AGE.value, "label": "Runs newer than"},
]
_UNITS: Final = [
    {"value": RetentionUnit.EXPERIMENT.value, "label": "of each experiment"},
    {"value": RetentionUnit.PROJECT.value, "label": "of the whole project"},
]


@dataclass(frozen=True)
class _Standing:
    """A project's retention settings, and how it stood when last checked: as the viewer may read them."""

    project: RetentionSetting
    experiments: dict[int, RetentionSetting]
    status: RetentionStatus | None

    @classmethod
    def of(cls, store: DataStore[...], project_id: int) -> _Standing:
        rules = store.get_scoped_settings(RetentionKey.RULE, project_id)
        status = store.get_scoped_settings(RetentionKey.STATUS, project_id).project
        return cls(
            project=RetentionSetting.model_validate(rules.project.value)
            if rules.project
            else RetentionSetting(),
            experiments={e: RetentionSetting.model_validate(s.value) for e, s in rules.experiments.items()},
            status=RetentionStatus.model_validate(status.value) if status else None,
        )

    def rules(self, now: datetime) -> Rules:
        """What the project is held to at `now`."""
        experiments = {e: r for e, s in self.experiments.items() if (r := s.in_effect(now)) is not None}
        return Rules(project=self.project.in_effect(now) or Unlimited(), experiments=experiments)

    @property
    def anything_pending(self) -> bool:
        return self.project.pending is not None or any(
            s.pending is not None for s in self.experiments.values()
        )


def rule_of(kind: RetentionKind, amount: int | None, unit: RetentionUnit) -> RetentionRule | str:
    """The rule the form says, or why it says none: a count or an age needs how many."""
    if kind is RetentionKind.UNLIMITED:
        return Unlimited()
    if amount is None or amount < 1:
        return "Say how many (1 or more)"
    match kind:
        case RetentionKind.RUN_COUNT:
            return RunCount(max_runs=amount, unit=unit)
        case RetentionKind.RUN_AGE:
            return RunAge(max_age_days=amount)


def _when(at: datetime) -> str:
    return pendulum.instance(at).format("D MMM YYYY")


def _described(rule: RetentionRule, settings: RetentionSettings) -> str:
    """
    A rule in words, and when it deletes.

    A count or an age deletes nothing until runs are its slack past it, so a project holding more runs than its rule
    says would otherwise look as if retention were not working.
    """
    match rule:
        case Unlimited():
            return describe(rule)
        case RunCount(max_runs=n, unit=unit):
            holder = "the project holds" if unit is RetentionUnit.PROJECT else "an experiment holds"
            return f"{describe(rule)} · deletes down to {n} once {holder} more than {n + settings.retention_run_slack}"
        case RunAge(max_age_days=days):
            past = days + settings.retention_age_slack_days
            return (
                f"{describe(rule)} · deletes down to {_days(days)} once a run is more than {_days(past)} old"
            )


def _days(count: int) -> str:
    return f"{count} day{'s' if count != 1 else ''}"


def _scope_line(
    label: str, setting: RetentionSetting, *, inherits: str, now: datetime, settings: RetentionSettings
) -> Component:
    """One scope's rule in effect, and the one pending."""
    current = setting.as_of(now)
    active = _described(current.active.rule, settings) if current.active is not None else inherits
    parts: list[Component] = [dmc.Text(label, fw=600, size="sm", w=160), dmc.Text(active, size="sm")]
    if current.pending is not None:
        parts.append(
            dmc.Badge(
                f"From {_when(current.pending.effective_at)}: {_described(current.pending.rule, settings)}",
                color="yellow",
                variant="light",
                size="sm",
                tt="none",
            )
        )
    return dmc.Group(parts, gap="xs", wrap="nowrap")


def _status(status: RetentionStatus | None, now: datetime, settings: RetentionSettings) -> list[Component]:
    """How the project stood when last checked, and a warning if it has held runs to delete for too long."""
    if status is None:
        return [dmc.Text("Not checked yet.", size="xs", c="dimmed")]
    checked = pendulum.instance(status.checked_at).diff_for_humans(pendulum.instance(now), absolute=True)
    overdue = f"{status.overdue_runs:,}{' or more' if status.overdue_runs >= OVERDUE_COUNTED else ''}"
    line = dmc.Text(
        f"Checked {checked} ago" + (f": {overdue} runs still to delete." if status.overdue_runs else "."),
        size="xs",
        c="dimmed",
    )
    since = status.out_of_spec_since
    warn_after = pendulum.duration(hours=settings.retention_warn_after_hours)
    if since is None or now - pendulum.instance(since) < warn_after:
        return [line]
    locked = status.is_locked(now, settings.lock_after)
    return [
        dmc.Alert(
            f"This project has held runs its rules say to delete since {_when(since)}: retention is catching up, a batch "
            "at a time. "
            + (
                "New runs are refused until it has caught up (runs already logging are not affected)."
                if locked
                else "An admin can let it delete more at once (DLBOARD_RETENTION_PROJECT_BUDGET)."
            ),
            color="red" if locked else "yellow",
            variant="light",
            title="Locked: behind on retention" if locked else "Behind on retention",
        ),
        line,
    ]


def _hints(settings: RetentionSettings) -> list[Component]:
    hints = [
        "A kept run (Keep, in a run list) is never deleted by retention. What retention deletes goes to the Trash first.",
    ]
    if settings.caps:
        allowed = " or ".join(describe(cap).removeprefix("Keeps ") for cap in settings.caps)
        hints.append(f"This server keeps at most {allowed}; only an admin goes past it.")
    return [dmc.Text(hint, size="xs", c="dimmed") for hint in hints]


def _owns(project_id: int) -> bool:
    """Whether the viewer may set the project's own rule: its owner, or an admin."""
    return has_scope(get_current_user(), Scope.ALL) or get_project_role(project_id) is ProjectRole.OWNER


def _scopes(
    store: DataStore[...], project_id: int, *, search: str = "", keep: str | None = None
) -> list[dict[str, str]]:
    """
    What the form can set a rule for: the project, if the viewer owns it, and its experiments.

    The newest of them, or those `search` finds, and the one picked (`keep`): an experiment picker's (#590).
    """
    experiments = experiment_options(
        store, project_id, search=search, keep=[] if keep in (None, PROJECT_SCOPE) else [keep]
    )
    return [*([{"value": PROJECT_SCOPE, "label": "This project"}] if _owns(project_id) else []), *experiments]


def _form(standing: _Standing, scopes: list[dict[str, str]], *, owner: bool) -> Component:
    """The form that sets a rule, for the project if the viewer owns it, else for one of its experiments."""
    latest = standing.project.latest()
    rule = latest.rule if latest is not None else Unlimited()
    match rule:
        case RunCount(max_runs=amount) | RunAge(max_age_days=amount):
            pass
        case Unlimited():
            amount = None
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Select(
                        id=SCOPE_ID,
                        data=scopes,
                        value=scopes[0]["value"] if scopes else None,
                        allowDeselect=False,
                        searchable=True,
                        size="xs",
                        w=180,
                        label="Rule for",
                    ),
                    experiment_search_store(SCOPE_ID),
                    dmc.Select(
                        id=KIND_ID,
                        data=_KINDS,
                        value=rule.kind.value,
                        allowDeselect=False,
                        size="xs",
                        w=160,
                        label="Keep",
                    ),
                    dmc.NumberInput(
                        id=AMOUNT_ID,
                        value=amount,
                        min=1,
                        size="xs",
                        w=100,
                        label="Runs or days",
                    ),
                    dmc.Select(
                        id=UNIT_ID,
                        data=_UNITS,
                        value=rule.unit.value
                        if isinstance(rule, RunCount)
                        else RetentionUnit.EXPERIMENT.value,
                        allowDeselect=False,
                        size="xs",
                        w=170,
                        label="Counted",
                        disabled=not owner,  # an experiment's own count is of its own runs: nothing to choose
                    ),
                    dmc.Button("Preview", id=PREVIEW_BUTTON_ID, size="xs", variant="default"),
                    dmc.Button("Save", id=SAVE_ID, size="xs"),
                    dmc.Button(
                        "Cancel pending change",
                        id=CANCEL_ID,
                        size="xs",
                        variant="default",
                        # In the page whatever is pending, as a callback listens to it; shown only while something is.
                        style=None if standing.anything_pending else {"display": "none"},
                    ),
                ],
                gap="xs",
                align="flex-end",
            ),
            dmc.Text(id=PREVIEW_ID, size="xs", c="dimmed"),
            dmc.Text(id=ERROR_ID, c="red", size="sm"),
        ],
        gap=4,
    )


def experiment_retention_summary(experiment: Experiment) -> Component:
    """
    The line under an experiment's header: its own rule, its project's, and a link to the project's tab that sets both.

    Shown on every experiment's page, so that the page runs are deleted from says what deletes them.
    """
    rules = get_data_store().get_scoped_settings(RetentionKey.RULE, experiment.project_id)
    own = _in_effect(rules.experiments.get(experiment.id))
    project = describe(_in_effect(rules.project) or Unlimited())
    return _summary(
        f"{describe(own) if own is not None else 'Follows the project'} · Project: {project}",
        f"{relative_path(f'/project/{experiment.project_id}')}?tab={RETENTION_TAB}",
        link="Change →",  # short: it shares the header's width with the page's controls
    )


def _in_effect(setting: ScopedSetting | None) -> RetentionRule | None:
    return (
        RetentionSetting.model_validate(setting.value).in_effect(pendulum.now(pendulum.UTC))
        if setting
        else None
    )


def _summary(rule: str, href: str, link: str) -> Component:
    return dmc.Group(
        [
            dmc.Text("Retention", size="sm", fw=600, c="dimmed"),
            dmc.Text(rule, size="sm"),
            html.A(link, href=href, style={"fontSize": "var(--mantine-font-size-sm)"}),
        ],
        gap="xs",
    )


def render_retention_section(project: Project) -> Component:
    """The Retention tab's content, in the container its Save and Cancel draw it again in."""
    return html.Div(_section(project), id=SECTION_ID)


def _section(project: Project) -> Component:
    store = get_data_store()
    standing = _Standing.of(store, project.id)
    role = get_project_role(project.id)
    owner = _owns(project.id)
    editor = role is not None and role.includes(ProjectRole.EDITOR)
    # The experiments with a rule of their own, by id: never every experiment of the project (#590).
    names = {
        e.id: experiment_display_name(e) for e in store.get_experiments_by_id(standing.experiments.keys())
    }
    settings = RetentionSettings()
    now = pendulum.now(pendulum.UTC)
    return dmc.Stack(
        [
            *_status(standing.status, now, settings),
            _scope_line(
                "This project", standing.project, inherits="Keeps every run", now=now, settings=settings
            ),
            *(
                _scope_line(
                    names.get(e, f"Experiment {e}"),
                    s,
                    inherits="Follows the project",
                    now=now,
                    settings=settings,
                )
                for e, s in standing.experiments.items()
            ),
            *_hints(settings),
            *([_form(standing, _scopes(store, project.id), owner=owner)] if editor or owner else []),
        ],
        gap="xs",
    )


def _experiment_of(scope: str | None) -> int | None:
    """The experiment the scope select names, or `None` for the project's own rule."""
    return None if scope in (None, PROJECT_SCOPE) else int(scope)


def _rule_from(kind: RetentionKind, amount: float | str | None, unit: RetentionUnit) -> RetentionRule | str:
    """`rule_of`, with the number input's value: a float, or "" when it is empty."""
    whole = int(amount) if isinstance(amount, int | float) and amount == int(amount) else None
    return rule_of(kind, whole, unit)


class _Form(CallbackStates):
    """What the form says when Preview is clicked: counting what a rule would delete is a probe of every experiment."""

    clicks: Annotated[int | None, Input(PREVIEW_BUTTON_ID, "n_clicks")]
    scope: Annotated[str | None, State(SCOPE_ID, "value")]
    kind: Annotated[RetentionKind, State(KIND_ID, "value")]
    amount: Annotated[float | str | None, State(AMOUNT_ID, "value")]
    unit: Annotated[RetentionUnit, State(UNIT_ID, "value")]
    project_id: Annotated[int, State(constants.STATE_PROJECT_ID, "data")]


class _Saved(CallbackStates):
    """What the form says when Save is clicked."""

    clicks: Annotated[int | None, Input(SAVE_ID, "n_clicks")]
    scope: Annotated[str | None, State(SCOPE_ID, "value")]
    kind: Annotated[RetentionKind, State(KIND_ID, "value")]
    amount: Annotated[float | str | None, State(AMOUNT_ID, "value")]
    unit: Annotated[RetentionUnit, State(UNIT_ID, "value")]
    project_id: Annotated[int, State(constants.STATE_PROJECT_ID, "data")]


def _with(rules: Rules, experiment_id: int | None, rule: RetentionRule) -> Rules:
    """`rules`, with `rule` in effect for the project (an experiment's own dropped if its kind changes) or an experiment."""
    if experiment_id is not None:
        return Rules(project=rules.project, experiments={**rules.experiments, experiment_id: rule})
    kept: Mapping[int, RetentionRule] = rules.experiments if rule.kind is rules.project.kind else {}
    return Rules(project=rule, experiments=kept)


def _preview(form: _Form) -> str:
    """How many runs the form's rule would delete now, and when it would take effect."""
    rule = _rule_from(form.kind, form.amount, form.unit)
    if isinstance(rule, str):
        return ""
    store, settings, now = get_data_store(), RetentionSettings(), pendulum.now(pendulum.UTC)
    rules = _with(_Standing.of(store, form.project_id).rules(now), _experiment_of(form.scope), rule)
    count = Candidates(store, settings, now).overdue(form.project_id, rules, limit=PREVIEW_LIMIT)
    runs = f"{count:,}{' or more' if count >= PREVIEW_LIMIT else ''} run{'s' if count != 1 else ''}"
    grace = settings.retention_grace_days
    when = f"from {_when(now.add(days=grace))}" if grace else "at once"
    return f"{_described(rule, settings)}: it would delete {runs} as things stand, {when}."


def _save(form: _Saved) -> str | None:
    """Save the form's rule (pending, for the grace period); why not, if it is refused."""
    rule = _rule_from(form.kind, form.amount, form.unit)
    if isinstance(rule, str):
        return rule
    store, user, now = get_data_store(), get_current_user(), pendulum.now(pendulum.UTC)
    standing = _Standing.of(store, form.project_id)
    experiment_id = _experiment_of(form.scope)
    latest = standing.project.latest()
    ceiling = latest.rule if latest is not None else Unlimited()
    if experiment_id is not None and not isinstance(rule, Unlimited) and not keeps_at_most(rule, ceiling):
        return f"An experiment keeps at most what its project does, with a rule of its kind ({ceiling.kind})"
    current = (
        standing.project
        if experiment_id is None
        else standing.experiments.get(experiment_id, RetentionSetting())
    )
    changed = current.saved(rule, by=user.id, now=now, grace=RetentionSettings().grace)
    no_rule_of_its_own = experiment_id is not None and changed.active is None and isinstance(rule, Unlimited)
    # "Every run" is no kind of limit: only a count becoming an age (or the reverse) says nothing of what was set before.
    changes_kind = {rule.kind, ceiling.kind}.isdisjoint(
        {RetentionKind.UNLIMITED}
    ) and rule.kind is not ceiling.kind
    try:
        value = None if no_rule_of_its_own else changed.dump()
        # The scope's own first: a save its check refuses (a cap) leaves the experiments' rules as they were.
        store.put_scoped_setting(RetentionKey.RULE, form.project_id, experiment_id, value, user)
        if experiment_id is None and changes_kind:
            for other in standing.experiments:
                store.put_scoped_setting(RetentionKey.RULE, form.project_id, other, None, user)
    except PermissionError as refused:
        return str(refused).removeprefix("Not allowed: ") or "Not yours to change."
    except ValueError as refused:
        return str(refused)
    return None


def _cancel(project_id: int, scope: str | None) -> str | None:
    """Drop the selected scope's pending rule; why not, if it is refused."""
    store, user, now = get_data_store(), get_current_user(), pendulum.now(pendulum.UTC)
    standing = _Standing.of(store, project_id)
    experiment_id = _experiment_of(scope)
    current = (
        standing.project
        if experiment_id is None
        else standing.experiments.get(experiment_id, RetentionSetting())
    )
    active = current.as_of(now).active
    try:
        store.put_scoped_setting(
            RetentionKey.RULE,
            project_id,
            experiment_id,
            RetentionSetting(active=active).dump() if active is not None else None,
            user,
        )
    except PermissionError:
        return "Not yours to change."
    return None


def _redrawn(project_id: int) -> Component:
    project = get_data_store().get_project(project_id)
    if project is None:
        raise PreventUpdate
    return _section(project)


def register_retention_callbacks(app: Dash) -> None:
    """Wire the Retention section's form: its preview, and Save and Cancel, which draw the section again."""
    outputs = (
        Output(SECTION_ID, "children", allow_duplicate=True),
        Output(ERROR_ID, "children", allow_duplicate=True),
    )

    @app.callback(Output(UNIT_ID, "disabled"), Input(SCOPE_ID, "value"), prevent_initial_call=True)
    def unit_applies(scope: str | None) -> bool:
        return _experiment_of(scope) is not None

    register_search_gate(app, SCOPE_ID)

    @app.callback(
        Output(SCOPE_ID, "data", allow_duplicate=True),
        Input(searched(SCOPE_ID), "data"),
        State(SCOPE_ID, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def search_scopes(search: str | None, scope: str | None, project_id: int) -> list[dict[str, str]]:
        """The experiments the form can set a rule for that a search finds, as someone types (#590)."""
        return _scopes(get_data_store(), project_id, search=search or "", keep=scope)

    @app.callback(Output(PREVIEW_ID, "children"), _Form.states(), prevent_initial_call=True)
    def preview(**states: Any) -> str:  # noqa: ANN401 -- validated on the first line
        form = _Form.model_validate(states)
        if not form.clicks:  # the section drawn again, with a new button
            raise PreventUpdate
        return _preview(form)

    app.clientside_callback(
        _CLEAR_PREVIEW_JS.source,
        Output(PREVIEW_ID, "children", allow_duplicate=True),
        Input(SCOPE_ID, "value"),
        Input(KIND_ID, "value"),
        Input(AMOUNT_ID, "value"),
        Input(UNIT_ID, "value"),
        prevent_initial_call=True,
    )

    @app.callback(*outputs, _Saved.states(), prevent_initial_call=True)
    def save(**states: Any) -> tuple[Any, Any]:  # noqa: ANN401 -- validated on the first line
        form = _Saved.model_validate(states)
        if not form.clicks:  # the section drawn again, with a new button
            raise PreventUpdate
        refused = _save(form)
        return (no_update, refused) if refused is not None else (_redrawn(form.project_id), no_update)

    @app.callback(
        *outputs,
        Input(CANCEL_ID, "n_clicks"),
        State(SCOPE_ID, "value"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def cancel(clicks: int | None, scope: str | None, project_id: int) -> tuple[Any, Any]:
        if not clicks:
            raise PreventUpdate
        refused = _cancel(project_id, scope)
        return (no_update, refused) if refused is not None else (_redrawn(project_id), no_update)
