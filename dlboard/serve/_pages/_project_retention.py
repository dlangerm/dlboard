"""
A project's Retention section: how many runs, or how old, it and each of its experiments keep, and how it stands.

One form sets a rule for the project (an owner's) or for one of its experiments (any editor's: stricter by construction,
see `DataStore.set_retention_policy`). A saved rule waits out the grace period, shown here as pending with what it would
delete, while the rule before it is enforced. Saving or cancelling draws the section again in place: every callback here
writes only inside the section, so none is called as the page draws it (each such one is a round trip a page load waits
on, `page_load_budget_test`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

import dash_mantine_components as dmc
import pendulum
from dash import Dash, Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from dlboard import models
from dlboard.models import ProjectRole, RetentionKind, RetentionUnit, Scope, has_scope
from dlboard.serve import _constants as constants
from dlboard.serve import get_current_user, get_data_store, get_project_role
from dlboard.serve._backend._retention_settings import OVERDUE_COUNTED, RetentionSettings
from dlboard.serve._component_ids import ButtonId, DivId, ValueId
from dlboard.serve._pages._dash_helpers import CallbackStates, section_label
from dlboard.serve._pages._dataframe_helpers import experiment_display_name

if TYPE_CHECKING:
    from datetime import datetime

    from dash.development.base_component import Component

    from dlboard.models import Project


class _RetentionSection:
    """Page tag: marks a component id as belonging to the project page's Retention section."""


SCOPE_ID: ValueId[_RetentionSection] = ValueId("retention-scope")
KIND_ID: ValueId[_RetentionSection] = ValueId("retention-kind")
AMOUNT_ID: ValueId[_RetentionSection] = ValueId("retention-amount")
UNIT_ID: ValueId[_RetentionSection] = ValueId("retention-unit")
SAVE_ID: ButtonId[_RetentionSection] = ButtonId("retention-save")
PREVIEW_ID: DivId[_RetentionSection] = DivId("retention-preview")
ERROR_ID: DivId[_RetentionSection] = DivId("retention-error")
CANCEL_ID: ButtonId[_RetentionSection] = ButtonId("retention-cancel")
SECTION_ID: DivId[_RetentionSection] = DivId("retention-section")

PROJECT_SCOPE: Final = "project"
"""The scope select's value for the project's own rule; an experiment's is its id."""
PREVIEW_LIMIT: Final = 1_000
"""How many runs the preview counts to: past that it says only that many or more."""

_KINDS: Final = [
    {"value": RetentionKind.UNLIMITED.value, "label": "Every run"},
    {"value": RetentionKind.RUN_COUNT.value, "label": "The newest runs"},
    {"value": RetentionKind.RUN_AGE.value, "label": "Runs newer than"},
]
_UNITS: Final = [
    {"value": RetentionUnit.EXPERIMENT.value, "label": "of each experiment"},
    {"value": RetentionUnit.PROJECT.value, "label": "of the whole project"},
]


_COUNTED_OVER: Final[dict[RetentionUnit, str]] = {
    RetentionUnit.EXPERIMENT: "each experiment",
    RetentionUnit.PROJECT: "the project",
}


def describe(rule: models.RetentionRule) -> str:
    """A rule, in words."""
    match rule:
        case models.Unlimited():
            return "Keeps every run"
        case models.RunCount(max_runs=n, unit=unit):
            return f"Keeps the newest {n} runs of {_COUNTED_OVER[unit]}"
        case models.RunAge(max_age_days=days):
            return f"Keeps runs from the last {days} day{'s' if days != 1 else ''}"


def rule_of(kind: RetentionKind, amount: int | None, unit: RetentionUnit) -> models.RetentionRule | str:
    """The rule the form says, or why it says none: a count or an age needs how many."""
    if kind is RetentionKind.UNLIMITED:
        return models.Unlimited()
    if amount is None or amount < 1:
        return "Say how many (1 or more)"
    match kind:
        case RetentionKind.RUN_COUNT:
            return models.RunCount(max_runs=amount, unit=unit)
        case RetentionKind.RUN_AGE:
            return models.RunAge(max_age_days=amount)


def _when(at: datetime) -> str:
    return pendulum.instance(at).format("D MMM YYYY")


def _scope_line(label: str, scope: models.ScopedRetention, *, inherits: str) -> Component:
    """One scope's rule in effect, and the one pending."""
    active = describe(scope.active.rule) if scope.active is not None else inherits
    parts: list[Component] = [dmc.Text(label, fw=600, size="sm", w=160), dmc.Text(active, size="sm")]
    if scope.pending is not None:
        parts.append(
            dmc.Badge(
                f"{describe(scope.pending.rule)} from {_when(scope.pending.effective_at)}",
                color="yellow",
                variant="light",
                size="sm",
                tt="none",
            )
        )
    return dmc.Group(parts, gap="xs", wrap="nowrap")


def _status(retention: models.ProjectRetention) -> list[Component]:
    """How the project stood when last checked, and a warning if it has held runs to delete for too long."""
    status = retention.status
    if status is None:
        return [dmc.Text("Not checked yet.", size="xs", c="dimmed")]
    now = pendulum.now("UTC")
    checked = pendulum.instance(status.checked_at).diff_for_humans(now, absolute=True)
    overdue = f"{status.overdue_runs:,}{' or more' if status.overdue_runs >= OVERDUE_COUNTED else ''}"
    line = dmc.Text(
        f"Checked {checked} ago" + (f": {overdue} runs still to delete." if status.overdue_runs else "."),
        size="xs",
        c="dimmed",
    )
    since = status.out_of_spec_since
    warn_after = pendulum.duration(hours=RetentionSettings().retention_warn_after_hours)
    if since is None or now - pendulum.instance(since) < warn_after:
        return [line]
    return [
        dmc.Alert(
            f"This project has held runs its rules say to delete since {_when(since)}: retention is catching up, a batch "
            "at a time. An admin can let it delete more at once (DLBOARD_RETENTION_SWEEP_BUDGET).",
            color="yellow",
            variant="light",
            title="Behind on retention",
        ),
        line,
    ]


def _cap_hint() -> list[Component]:
    caps = RetentionSettings().caps
    if not caps:
        return []
    allowed = " or ".join(describe(cap).removeprefix("Keeps ") for cap in caps)
    return [
        dmc.Text(f"This server keeps at most {allowed}; only an admin goes past it.", size="xs", c="dimmed")
    ]


def _anything_pending(retention: models.ProjectRetention) -> bool:
    return retention.project.pending is not None or any(
        s.pending is not None for s in retention.experiments.values()
    )


def _slack_hint() -> str:
    """Why a rule may delete nothing yet: it acts in batches, once past its slack."""
    settings = RetentionSettings()
    days = settings.retention_age_slack_days
    return (
        f"Runs are deleted in batches: once a count is {settings.retention_run_slack} runs past, down to the count, and once a run is "
        f"{days} day{'s' if days != 1 else ''} past an age, every run older than it."
    )


def _form(project: Project, retention: models.ProjectRetention, *, owner: bool) -> Component:
    """The form that sets a rule, for the project if the viewer owns it, else for one of its experiments."""
    experiments = list(get_data_store().get_experiments(project.id))
    scopes = [
        *([{"value": PROJECT_SCOPE, "label": "This project"}] if owner else []),
        *({"value": str(e.id), "label": experiment_display_name(e)} for e in experiments),
    ]
    latest = retention.project.pending or retention.project.active
    rule = latest.rule if latest is not None else models.Unlimited()
    match rule:
        case models.RunCount(max_runs=amount) | models.RunAge(max_age_days=amount):
            pass
        case models.Unlimited():
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
                        size="xs",
                        w=180,
                        label="Rule for",
                    ),
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
                        id=AMOUNT_ID, value=amount, min=1, size="xs", w=100, label="Runs or days"
                    ),
                    dmc.Select(
                        id=UNIT_ID,
                        data=_UNITS,
                        value=rule.unit.value
                        if isinstance(rule, models.RunCount)
                        else RetentionUnit.EXPERIMENT.value,
                        allowDeselect=False,
                        size="xs",
                        w=170,
                        label="Counted",
                    ),
                    dmc.Button("Save", id=SAVE_ID, size="xs"),
                    dmc.Button(
                        "Cancel pending change",
                        id=CANCEL_ID,
                        size="xs",
                        variant="default",
                        # In the page whatever is pending, as a callback listens to it; shown only while something is.
                        style=None if _anything_pending(retention) else {"display": "none"},
                    ),
                ],
                gap="xs",
                align="flex-end",
            ),
            dmc.Text(id=PREVIEW_ID, size="xs", c="dimmed"),
            dmc.Text(_slack_hint(), size="xs", c="dimmed"),
            dmc.Text(id=ERROR_ID, c="red", size="sm"),
        ],
        gap=4,
    )


def render_retention_section(project: Project) -> Component:
    """The Retention section, in the container its Save and Cancel draw it again in."""
    return html.Div(_section(project), id=SECTION_ID)


def _section(project: Project) -> Component:
    """What the project and its experiments keep, how it stands, and (for an editor) the form."""
    store = get_data_store()
    retention = store.get_retention(project.id)
    role = get_project_role(project.id)
    owner = has_scope(get_current_user(), Scope.ALL) or role is ProjectRole.OWNER
    editor = role is not None and role.includes(ProjectRole.EDITOR)
    names = {e.id: experiment_display_name(e) for e in store.get_experiments(project.id)}
    return dmc.Stack(
        [
            section_label("Retention"),
            *_status(retention),
            _scope_line(
                "This project",
                retention.project,
                inherits="Keeps every run",
            ),
            *(
                _scope_line(
                    names.get(experiment_id, f"Experiment {experiment_id}"),
                    scope,
                    inherits="Follows the project",
                )
                for experiment_id, scope in retention.experiments.items()
            ),
            dmc.Text(
                "A kept run (Keep, in a run list) is never deleted by retention. What retention deletes goes to the "
                "Trash first.",
                size="xs",
                c="dimmed",
            ),
            *_cap_hint(),
            *([_form(project, retention, owner=owner)] if editor or owner else []),
        ],
        gap="xs",
        mt="xl",
    )


def _experiment_of(scope: str | None) -> int | None:
    """The experiment the scope select names, or `None` for the project's own rule."""
    return None if scope in (None, PROJECT_SCOPE) else int(scope)


def _rule_from(
    kind: RetentionKind, amount: float | str | None, unit: RetentionUnit
) -> models.RetentionRule | str:
    """The rule the form says: `rule_of`, with the number input's value (a float, or "" when it is empty)."""
    whole = int(amount) if isinstance(amount, int | float) and amount == int(amount) else None
    return rule_of(kind, whole, unit)


class _Form(CallbackStates):
    """What the form says, as it changes: for the preview."""

    scope: Annotated[str | None, Input(SCOPE_ID, "value")]
    kind: Annotated[RetentionKind, Input(KIND_ID, "value")]
    amount: Annotated[float | str | None, Input(AMOUNT_ID, "value")]
    unit: Annotated[RetentionUnit, Input(UNIT_ID, "value")]
    project_id: Annotated[int, State(constants.STATE_PROJECT_ID, "data")]


class _Saved(CallbackStates):
    """What the form says when Save is clicked (its click gate's store)."""

    clicks: Annotated[int | None, Input(SAVE_ID, "n_clicks")]
    scope: Annotated[str | None, State(SCOPE_ID, "value")]
    kind: Annotated[RetentionKind, State(KIND_ID, "value")]
    amount: Annotated[float | str | None, State(AMOUNT_ID, "value")]
    unit: Annotated[RetentionUnit, State(UNIT_ID, "value")]
    project_id: Annotated[int, State(constants.STATE_PROJECT_ID, "data")]


def _preview(form: _Form) -> str:
    """How many runs the form's rule would delete now, and when it would take effect."""
    rule = _rule_from(form.kind, form.amount, form.unit)
    if isinstance(rule, str):
        return ""
    store = get_data_store()
    count = store.retention_preview(form.project_id, _experiment_of(form.scope), rule, limit=PREVIEW_LIMIT)
    runs = f"{count:,}{' or more' if count >= PREVIEW_LIMIT else ''} run{'s' if count != 1 else ''}"
    grace = RetentionSettings().retention_grace_days
    when = f"from {_when(pendulum.now('UTC').add(days=grace))}" if grace else "at once"
    return f"{describe(rule)}: it would delete {runs} as things stand, {when}."


def _save(form: _Saved) -> str | None:
    """Save the form's rule; why not, if it is refused."""
    rule = _rule_from(form.kind, form.amount, form.unit)
    if isinstance(rule, str):
        return rule
    experiment_id = _experiment_of(form.scope)
    try:
        get_data_store().set_retention_policy(form.project_id, experiment_id, rule, get_current_user())
    except PermissionError:
        if experiment_id is None and not RetentionSettings().within_caps(rule):
            return "That keeps more than this server allows: only an admin can."
        return "Not yours to change."
    except ValueError as refused:
        return str(refused)
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

    @app.callback(Output(PREVIEW_ID, "children"), _Form.states(), prevent_initial_call=True)
    def preview(**states: Any) -> str:  # noqa: ANN401 -- validated on the first line
        return _preview(_Form.model_validate(states))

    @app.callback(*outputs, _Saved.states(), prevent_initial_call=True)
    def save(**states: Any) -> tuple[Any, Any]:  # noqa: ANN401 -- validated on the first line
        form = _Saved.model_validate(states)
        if not form.clicks:  # the section drawn again, with a new button
            raise PreventUpdate
        refused = _save(form)
        if refused is not None:
            return no_update, refused
        return _redrawn(form.project_id), no_update

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
        try:
            get_data_store().cancel_pending_retention(project_id, _experiment_of(scope), get_current_user())
        except PermissionError:
            return no_update, "Not yours to change."
        return _redrawn(project_id), no_update


RETENTION_ANCHOR: Final = "retention-section"
"""The fragment a project page's Retention link goes to: the section's own id, which is also its anchor."""


def retention_summary(project_id: int) -> Component:
    """
    One line under the project's header: the rule its runs are kept by, and a link to the section that sets it.

    Shown whether or not a rule is set, so that retention is something a project's page says it can do: "Keeps every run"
    is the state a project is in until someone sets a rule.
    """
    retention = get_data_store().get_retention(project_id)
    active = retention.project.active
    current = describe(active.rule) if active is not None else "Keeps every run"
    return dmc.Group(
        [
            dmc.Text("Retention", size="sm", fw=600, c="dimmed"),
            dmc.Text(current, size="sm"),
            html.A(
                "Set how many runs are kept →",
                href=f"#{RETENTION_ANCHOR}",
                style={"fontSize": "var(--mantine-font-size-sm)"},
            ),
        ],
        gap="xs",
        mb="lg",
    )
