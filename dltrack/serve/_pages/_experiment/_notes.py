"""
An experiment's notes: a thread of comments in a side drawer, each optionally about runs or people.

Costs one count query per page load and nothing more until opened: the thread and the run/people
pickers load when the drawer opens. It stays current without a polling loop of its own -- posting
or deleting a note bumps `Experiment.notes_revision`, which the experiment page's existing live
poll already reads (see `poll_for_updates`) and hands to `STATE_NOTES_REVISION` only when it
changed.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pendulum
from dash import ALL, Input, Output, State, ctx, html, no_update
from dash.exceptions import PreventUpdate

from dltrack import models
from dltrack.models import StoreId, constants
from dltrack.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon, series_swatch_class
from dltrack.serve._pages._dash_helpers import require_triggered_id, tooltipped_action_icon

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from dltrack.models import DataStore
    from dltrack.serve._pages._experiment import _experiment_page_state as core

STATE_NOTES_REVISION: StoreId[core.ExperimentPage] = StoreId("notes-revision-state")
"""The `Experiment.notes_revision` this page last showed; the live poll updates it on a change."""

NOTES_OPEN_ID: typing.Final = "notes-open"
NOTES_COUNT_ID: typing.Final = "notes-count"
NOTES_DRAWER_ID: typing.Final = "notes-drawer"
NOTES_THREAD_ID: typing.Final = "notes-thread"
NOTES_BODY_ID: typing.Final = "notes-body"
NOTES_RUNS_ID: typing.Final = "notes-runs"
NOTES_MENTIONS_ID: typing.Final = "notes-mentions"
NOTES_POST_ID: typing.Final = "notes-post"
_DELETE_NOTE_TYPE: typing.Final = "delete-note"

_OPEN_ON_CLICK_JS = ClientsideScript(Path(__file__).with_name("open_on_click.js"))


def notes_button(count: int) -> dmc.Button:
    """The header's "Notes" button, with how many there are."""
    return dmc.Button(
        "Notes",
        id=NOTES_OPEN_ID,
        leftSection=icon(Icon.NOTES),
        rightSection=dmc.Badge(str(count), id=NOTES_COUNT_ID, size="xs", variant="light", circle=True),
        variant="subtle",
        color="gray",
        size="xs",
    )


def notes_drawer() -> dmc.Drawer:
    """The thread and its composer; filled in when opened."""
    return dmc.Drawer(
        id=NOTES_DRAWER_ID,
        title="Notes",
        position="right",
        size="md",
        opened=False,
        children=dmc.Stack(
            [
                html.Div(id=NOTES_THREAD_ID),
                dmc.Divider(),
                dmc.Textarea(
                    id=NOTES_BODY_ID,
                    placeholder="Write a note…",
                    autosize=True,
                    minRows=3,
                    **cast("dict[str, Any]", {"aria-label": "Note"}),
                ),
                dmc.MultiSelect(
                    id=NOTES_RUNS_ID,
                    placeholder="About runs…",
                    data=[],
                    value=[],
                    searchable=True,
                    size="xs",
                    comboboxProps={"withinPortal": False},
                ),
                dmc.MultiSelect(
                    id=NOTES_MENTIONS_ID,
                    placeholder="Mention people…",
                    data=[],
                    value=[],
                    searchable=True,
                    size="xs",
                    comboboxProps={"withinPortal": False},
                ),
                dmc.Group(
                    dmc.Button("Post", id=NOTES_POST_ID, leftSection=icon(Icon.NOTES)), justify="flex-end"
                ),
            ]
        ),
    )


def _run_label(run: models.Run) -> str:
    return run.name or f"Run {run.id}"


def _note(
    comment: models.Comment,
    users: dict[int, models.User],
    runs: dict[int, models.Run],
    me: models.User,
) -> Component:
    author = users[comment.author_id].username if comment.author_id in users else "unknown"
    chips = [
        *(
            dmc.Badge(
                _run_label(runs[run_id]),
                variant="default",
                leftSection=html.Span(className=series_swatch_class(run_id)),
                tt="none",
            )
            for run_id in comment.run_ids
            if run_id in runs
        ),
        *(
            dmc.Badge(f"@{users[user_id].username}", variant="light", tt="none")
            for user_id in comment.mentioned_user_ids
            if user_id in users
        ),
    ]
    written = pendulum.instance(comment.created_at).diff_for_humans(pendulum.now("UTC"), absolute=True)
    return dmc.Paper(
        [
            dmc.Group(
                [
                    dmc.Avatar(name=author, color="initials", size="sm", radius="xl"),
                    dmc.Text(author, size="sm", fw=600),
                    dmc.Text(f"{written} ago", size="xs", c="dimmed", style={"flex": 1}),
                    tooltipped_action_icon(
                        Icon.DELETE,
                        component_id={"type": _DELETE_NOTE_TYPE, "note": comment.id},
                        label="Delete this note",
                        color="red",
                        size="xs",
                    )
                    if comment.author_id == me.id
                    else None,
                ],
                gap="xs",
                wrap="nowrap",
            ),
            dmc.Text(comment.body, size="sm", mt=6, style={"whiteSpace": "pre-wrap"}),
            dmc.Group(chips, gap=4, mt=6) if chips else None,
        ],
        withBorder=True,
        p="sm",
    )


def _thread(
    store: DataStore[...], experiment_id: int
) -> tuple[Component, int, list[models.Run], list[models.User]]:
    """The rendered thread, how many notes it has, and the runs and users it (and the pickers) name."""
    comments = store.list_comments(experiment_id)
    users = store.list_users()
    runs = list(store.get_runs(experiment_id))
    me = get_current_user()
    users_by_id, runs_by_id = {u.id: u for u in users}, {r.id: r for r in runs}
    thread = (
        dmc.Stack([_note(c, users_by_id, runs_by_id, me) for c in comments], gap="xs")
        if comments
        else dmc.Text("No notes yet. Start the thread below.", size="sm", c="dimmed")
    )
    return thread, len(comments), runs, users


def register_notes_callbacks(app: Dash) -> None:
    """Wire the notes drawer: open, load/refresh, post, delete."""
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _OPEN_ON_CLICK_JS.source,
        Output(NOTES_DRAWER_ID, "opened"),
        Input(NOTES_OPEN_ID, "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(NOTES_THREAD_ID, "children"),
        Output(NOTES_COUNT_ID, "children"),
        Output(NOTES_RUNS_ID, "data"),
        Output(NOTES_MENTIONS_ID, "data"),
        Input(NOTES_DRAWER_ID, "opened"),
        Input(STATE_NOTES_REVISION, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def load_notes(opened: bool, _revision: int | None, experiment_id: int) -> tuple[Any, ...]:  # noqa: FBT001
        if ctx.triggered_id == NOTES_DRAWER_ID and not opened:  # pyright: ignore[reportUnknownMemberType]
            raise PreventUpdate
        store = get_data_store()
        if not opened:
            # A note came or went while the drawer's closed: just keep the button's count honest.
            return no_update, str(len(store.list_comments(experiment_id))), no_update, no_update
        thread, count, runs, users = _thread(store, experiment_id)
        return (
            thread,
            str(count),
            [{"value": str(r.id), "label": _run_label(r)} for r in runs],
            [{"value": str(u.id), "label": u.username} for u in users],
        )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(NOTES_THREAD_ID, "children", allow_duplicate=True),
        Output(NOTES_COUNT_ID, "children", allow_duplicate=True),
        Output(NOTES_BODY_ID, "value"),
        Output(NOTES_BODY_ID, "error"),
        Output(NOTES_RUNS_ID, "value"),
        Output(NOTES_MENTIONS_ID, "value"),
        Input(NOTES_POST_ID, "n_clicks"),
        State(NOTES_BODY_ID, "value"),
        State(NOTES_RUNS_ID, "value"),
        State(NOTES_MENTIONS_ID, "value"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def post_note(
        n_clicks: int | None, body: str | None, run_ids: list[str], user_ids: list[str], experiment_id: int
    ) -> tuple[Any, ...]:
        if not n_clicks:
            raise PreventUpdate
        if not body or not body.strip():
            return no_update, no_update, no_update, "Write something first", no_update, no_update
        store = get_data_store()
        store.add_comment(
            models.NewComment(
                experiment_id=experiment_id,
                author_id=get_current_user().id,
                body=body.strip(),
                run_ids=[int(r) for r in run_ids or []],
                mentioned_user_ids=[int(u) for u in user_ids or []],
            )
        )
        thread, count, _runs, _users = _thread(store, experiment_id)
        return thread, str(count), "", None, [], []

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(NOTES_THREAD_ID, "children", allow_duplicate=True),
        Output(NOTES_COUNT_ID, "children", allow_duplicate=True),
        Input({"type": _DELETE_NOTE_TYPE, "note": ALL}, "n_clicks"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def delete_note(_n_clicks: list[int | None], experiment_id: int) -> tuple[Any, str]:
        note_id = cast("dict[str, int]", require_triggered_id())["note"]
        store = get_data_store()
        store.delete_comment(note_id, get_current_user().id)
        thread, count, _runs, _users = _thread(store, experiment_id)
        return thread, str(count)
