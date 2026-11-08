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

from dlboard import models
from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon, series_swatch_class
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import StoreId
from dlboard.serve._pages._dash_helpers import TriggeredId, require_triggered_id, tooltipped_action_icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._pages._experiment._run_picker import register_run_search, run_options

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dash import Dash
    from dash.development.base_component import Component

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment import _ids as ids

STATE_NOTES_REVISION: StoreId[ids.ExperimentPage] = StoreId("notes-revision-state")
"""The `Experiment.notes_revision` this page last showed; the live poll updates it on a change."""

NOTES_OPEN_ID: typing.Final = "notes-open"
NOTES_COUNT_ID: typing.Final = "notes-count"
NOTES_DRAWER_ID: typing.Final = "notes-drawer"
NOTES_THREAD_ID: typing.Final = "notes-thread"
NOTES_BODY_ID: typing.Final = "notes-body"
NOTES_RUNS_ID: typing.Final = "notes-runs"
NOTES_MENTIONS_ID: typing.Final = "notes-mentions"
NOTES_POST_ID: typing.Final = "notes-post"
MENTION_LIMIT: typing.Final = 50
"""The most people the mention picker lists at once: the first by name, or the ones its search finds."""
_DELETE_NOTE_TYPE: typing.Final = "delete-note"


class _NoteButton(TriggeredId):
    note: int


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
                run_display_name(runs[run_id]),
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


def _mention_options(
    store: DataStore[...], *, search: str = "", keep: Iterable[str] = ()
) -> list[dict[str, str]]:
    """The people a note can mention: those it has picked (`keep`, as values) and the first `MENTION_LIMIT` `search` finds."""
    found = store.get_users(name_contains=search, limit=MENTION_LIMIT)
    kept = frozenset(int(value) for value in keep) - {user.id for user in found}
    picked = store.get_users(kept) if kept else list[models.User]()
    return [{"value": str(user.id), "label": user.username} for user in [*picked, *found]]


def _thread(store: DataStore[...], experiment_id: int) -> tuple[Component, int]:
    """The rendered thread and how many notes it has."""
    comments = store.list_comments(experiment_id)
    # Only the runs and people the notes are about: an experiment's runs (and a site's users) are unbounded, its notes' are
    # what they name.
    about = frozenset(run_id for comment in comments for run_id in comment.run_ids)
    runs = list(store.get_runs(experiment_id, limit=None, run_ids=about)) if about else list[models.Run]()
    named = frozenset(
        user_id for comment in comments for user_id in (comment.author_id, *comment.mentioned_user_ids)
    )
    me = get_current_user()
    users_by_id, runs_by_id = {u.id: u for u in store.get_users(named)}, {r.id: r for r in runs}
    thread = (
        dmc.Stack([_note(c, users_by_id, runs_by_id, me) for c in comments], gap="xs")
        if comments
        else dmc.Text("No notes yet. Start the thread below.", size="sm", c="dimmed")
    )
    return thread, len(comments)


def register_notes_callbacks(app: Dash) -> None:
    """Wire the notes drawer: open, load/refresh, post, delete."""
    register_run_search(app, NOTES_RUNS_ID)
    app.clientside_callback(
        _OPEN_ON_CLICK_JS.source,
        Output(NOTES_DRAWER_ID, "opened"),
        Input(NOTES_OPEN_ID, "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output(NOTES_THREAD_ID, "children"),
        Output(NOTES_COUNT_ID, "children"),
        Output(NOTES_RUNS_ID, "data", allow_duplicate=True),
        Output(NOTES_MENTIONS_ID, "data"),
        Input(NOTES_DRAWER_ID, "opened"),
        Input(STATE_NOTES_REVISION, "data"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def load_notes(opened: bool, _revision: int | None, experiment_id: int) -> tuple[Any, ...]:  # noqa: FBT001
        if ctx.triggered_id == NOTES_DRAWER_ID and not opened:
            raise PreventUpdate
        store = get_data_store()
        if not opened:
            # A note came or went while the drawer's closed: just keep the button's count honest.
            return no_update, str(len(store.list_comments(experiment_id))), no_update, no_update
        thread, count = _thread(store, experiment_id)
        return thread, str(count), run_options(store, experiment_id), _mention_options(store)

    @app.callback(
        Output(NOTES_MENTIONS_ID, "data", allow_duplicate=True),
        Input(NOTES_MENTIONS_ID, "searchValue"),
        State(NOTES_MENTIONS_ID, "value"),
        prevent_initial_call=True,
    )
    def search_people(search: str | None, picked: list[str] | None) -> list[dict[str, str]]:
        return _mention_options(get_data_store(), search=search or "", keep=picked or ())

    @app.callback(
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
        thread, count = _thread(store, experiment_id)
        return thread, str(count), "", None, [], []

    @app.callback(
        Output(NOTES_THREAD_ID, "children", allow_duplicate=True),
        Output(NOTES_COUNT_ID, "children", allow_duplicate=True),
        Input({"type": _DELETE_NOTE_TYPE, "note": ALL}, "n_clicks"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def delete_note(_n_clicks: list[int | None], experiment_id: int) -> tuple[Any, str]:
        note_id = require_triggered_id(_NoteButton).note
        store = get_data_store()
        store.delete_comment(note_id, get_current_user().id)
        thread, count = _thread(store, experiment_id)
        return thread, str(count)
