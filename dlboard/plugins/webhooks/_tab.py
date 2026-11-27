"""
The project page's Webhooks tab, for its owners: the project's webhooks, a form to add one, and Delete or Turn on for each.

A webhook's secret is made by the server and shown once, as the webhook is added; the tab never draws it again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final

import dash_mantine_components as dmc
from dash import ALL, Dash, Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
from flask import request
from pydantic import HttpUrl, ValidationError

from dlboard._compat import StrEnum
from dlboard.models import ProjectRole, Scope, SettingConflictError, has_scope
from dlboard.plugins.webhooks._hooks import (
    SIGNATURE_HEADER,
    ProjectWebhooks,
    RefusedError,
    Webhook,
    WebhookEvent,
    WebhookKey,
    WebhookSettings,
    check_url,
    link_base,
)
from dlboard.serve import SettingWrite, get_current_user, get_data_store, get_project_role, relative_path
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ValueId
from dlboard.serve._pages._dash_helpers import CallbackStates, TriggeredId, require_triggered_id

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import Project, ScopedSetting


class _WebhooksTab:
    """Page tag: marks a component id as belonging to the project page's Webhooks tab."""


URL_ID: ValueId[_WebhooksTab] = ValueId("webhooks-url")
EVENTS_ID: ValueId[_WebhooksTab] = ValueId("webhooks-events")
ADD_ID: ButtonId[_WebhooksTab] = ButtonId("webhooks-add")
ERROR_ID: DivId[_WebhooksTab] = DivId("webhooks-error")
NOTICE_ID: DivId[_WebhooksTab] = DivId("webhooks-notice")
SECTION_ID: DivId[_WebhooksTab] = DivId("webhooks-section")
WEBHOOKS_TAB: Final = "webhooks"
# Pattern-matched "type" discriminator, not a standalone component id -- a plain string.
_ACTION_TYPE: Final = "webhooks-action"

_DEFAULT_EVENTS: Final = [WebhookEvent.FINISHED, WebhookEvent.FAILED, WebhookEvent.CRASHED]


class _Action(StrEnum):
    DELETE = "delete"
    TURN_ON = "turn-on"


class _ActionButton(TriggeredId):
    hook: str
    action: _Action


def _owns(project_id: int) -> bool:
    return has_scope(get_current_user(), Scope.ALL) or get_project_role(project_id) is ProjectRole.OWNER


@dataclass(frozen=True)
class _Stored:
    """A project's webhooks as this request read them: what a change is made from, and saved over only if still there."""

    project_id: int
    setting: ScopedSetting | None

    @classmethod
    def read(cls, project_id: int) -> _Stored:
        return cls(project_id, get_data_store().get_scoped_settings(WebhookKey.HOOKS, project_id).project)

    @property
    def hooks(self) -> ProjectWebhooks:
        return (
            ProjectWebhooks.model_validate(self.setting.value)
            if self.setting is not None
            else ProjectWebhooks()
        )

    def save(self, changed: ProjectWebhooks) -> str | None:
        """Store `changed` as the project's webhooks, unless they changed since they were read; why not, if refused."""
        try:
            get_data_store().put_scoped_setting(
                WebhookKey.HOOKS,
                self.project_id,
                None,
                changed.model_dump(mode="json"),
                get_current_user(),
                expected=self.setting,
            )
        except PermissionError:
            return "Only an owner of the project changes its webhooks."
        except SettingConflictError:
            return "This project's webhooks changed just now (another owner, or the server turning one off): try again."
        except ValueError as refused:
            return str(refused)
        return None


def _row(hook: Webhook) -> Component:
    def button(label: str, action: _Action, color: str) -> Component:
        return dmc.Button(
            label,
            id={"type": _ACTION_TYPE, "hook": hook.id, "action": action},
            size="xs",
            color=color,
            variant="light",
        )

    status = (
        dmc.Tooltip(
            dmc.Badge("Off", color="red", variant="light", size="sm"), label=hook.turned_off, multiline=True
        )
        if hook.turned_off is not None
        else dmc.Badge("On", color="green", variant="light", size="sm")
    )
    return dmc.TableTr(
        [
            dmc.TableTd(dmc.Code(str(hook.url)), style={"wordBreak": "break-all"}),
            dmc.TableTd(", ".join(e.words.label for e in WebhookEvent if e in hook.events)),
            dmc.TableTd(status),
            dmc.TableTd(
                dmc.Group(
                    [
                        *(
                            [button("Turn on", _Action.TURN_ON, "green")]
                            if hook.turned_off is not None
                            else []
                        ),
                        button("Delete", _Action.DELETE, "red"),
                    ],
                    gap="xs",
                )
            ),
        ]
    )


def _section(project: Project) -> Component:
    hooks = _Stored.read(project.id).hooks.hooks
    table = dmc.Table(
        [
            dmc.TableThead(dmc.TableTr([dmc.TableTh(h) for h in ("Posts to", "When a run", "Status", "")])),
            dmc.TableTbody([_row(hook) for hook in hooks]),
        ]
    )
    return dmc.Stack(
        [
            dmc.Text(
                "When a run of this project ends, the server posts it as JSON to each webhook that asks for how it "
                f"ended, signed with the webhook's secret ({SIGNATURE_HEADER}). Its `text` is all a Slack incoming "
                "webhook needs. A webhook the server turned off is sent nothing that ends until it is turned on again.",
                size="sm",
                c="dimmed",
            ),
            table if hooks else dmc.Text("No webhooks yet.", size="sm", c="dimmed"),
            dmc.Group(
                [
                    dmc.TextInput(id=URL_ID, placeholder="https://hooks.slack.com/services/...", w=380),
                    dmc.MultiSelect(
                        id=EVENTS_ID,
                        data=[{"value": e, "label": e.words.label} for e in WebhookEvent],
                        value=list(_DEFAULT_EVENTS),
                        w=320,
                    ),
                    dmc.Button("Add webhook", id=ADD_ID),
                ],
                align="end",
            ),
        ],
        gap="sm",
    )


def render_webhooks_tab(project: Project) -> Component | None:
    """The tab, for an owner of the project (or an admin); no tab for anyone else."""
    if not _owns(project.id):
        return None
    return html.Div(
        [
            html.Div(id=NOTICE_ID),
            dmc.Text(id=ERROR_ID, c="red", size="sm"),
            html.Div(_section(project), id=SECTION_ID),
        ]
    )


def _secret_notice(hook: Webhook) -> Component:
    """The only time a webhook's secret is ever shown."""
    return dmc.Alert(
        [
            dmc.Text(
                "Copy the secret now -- it won't be shown again. It signs every post:", size="sm", mb="xs"
            ),
            dmc.Code(hook.secret, block=True, style={"wordBreak": "break-all"}),
        ],
        color="green",
        variant="light",
    )


class _Added(CallbackStates):
    clicks: Annotated[int | None, Input(ADD_ID, "n_clicks")]
    url: Annotated[str | None, State(URL_ID, "value")]
    events: Annotated[list[WebhookEvent] | None, State(EVENTS_ID, "value")]
    project_id: Annotated[int, State(constants.STATE_PROJECT_ID, "data")]


def _add(form: _Added) -> tuple[Webhook | None, str | None]:
    """The webhook the form adds, or why not."""
    if not form.events:
        return None, "Pick at least one way a run ends."
    try:
        url = HttpUrl((form.url or "").strip())
        check_url(url)
    except ValidationError:
        return None, "That isn't a URL."
    except RefusedError as refused:
        return None, str(refused)
    stored = _Stored.read(form.project_id)
    hook = Webhook(
        url=url,
        events=frozenset(form.events),
        link_base=link_base(request.host_url, relative_path("/")),
        created_by=get_current_user().id,
    )
    refused = stored.save(ProjectWebhooks(hooks=(*stored.hooks.hooks, hook)))
    return (None, refused) if refused is not None else (hook, None)


def _redrawn(project_id: int) -> Component:
    project = get_data_store().get_project(project_id)
    if project is None:
        raise PreventUpdate
    return _section(project)


def register_webhook_callbacks(app: Dash) -> None:
    """Wire the tab's Add, and each webhook's Delete and Turn on, which draw the list again."""
    outputs = (
        Output(SECTION_ID, "children", allow_duplicate=True),
        Output(ERROR_ID, "children", allow_duplicate=True),
        Output(NOTICE_ID, "children", allow_duplicate=True),
    )

    @app.callback(*outputs, _Added.states(), prevent_initial_call=True)
    def add(**states: Any) -> tuple[Any, Any, Any]:  # noqa: ANN401 -- validated on the first line
        form = _Added.model_validate(states)
        if not form.clicks:  # the section drawn again, with a new button
            raise PreventUpdate
        hook, refused = _add(form)
        if hook is None:
            return no_update, refused, None
        return _redrawn(form.project_id), None, _secret_notice(hook)

    @app.callback(
        *outputs,
        Input({"type": _ACTION_TYPE, "hook": ALL, "action": ALL}, "n_clicks"),
        State(constants.STATE_PROJECT_ID, "data"),
        prevent_initial_call=True,
    )
    def act(_clicks: list[int | None], project_id: int) -> tuple[Any, Any, Any]:
        pressed = require_triggered_id(_ActionButton)
        stored = _Stored.read(project_id)
        hooks = stored.hooks
        hook = hooks.find(pressed.hook)
        if hook is None:
            return _redrawn(project_id), None, None
        match pressed.action:
            case _Action.DELETE:
                changed = hooks.replaced(hook.id, None)
            case _Action.TURN_ON:
                changed = hooks.replaced(hook.id, hook.model_copy(update={"turned_off": None}))
        refused = stored.save(changed)
        return (no_update, refused, None) if refused is not None else (_redrawn(project_id), None, None)


def checked(write: SettingWrite) -> dict[str, Any] | None:
    """
    A project's webhooks as an owner saves them: no more than the server allows, each new URL one it may post to.

    Only a URL the project didn't have is resolved: deleting one webhook doesn't wait on, or fail by, another's DNS.
    """
    if write.experiment_id is not None:
        msg = "Webhooks are a project's, not an experiment's."
        raise PermissionError(msg)
    if write.value is None:
        return None
    hooks = ProjectWebhooks.model_validate(write.value)
    most = WebhookSettings().max_per_project
    if len(hooks.hooks) > most:
        msg = f"A project has at most {most} webhooks (DLBOARD_WEBHOOK_MAX_PER_PROJECT)."
        raise ValueError(msg)
    known = {str(h.url) for h in ProjectWebhooks.model_validate(write.stored or {}).hooks}
    for hook in hooks.hooks:
        if str(hook.url) not in known:
            try:
                check_url(hook.url)
            except RefusedError as refused:
                raise ValueError(str(refused)) from refused
    return hooks.model_dump(mode="json")
