"""A project page: its experiments, comparisons, sharing and plugins' tabs, plus an inline "new experiment" field."""

from __future__ import annotations

import typing
from pathlib import Path
from typing import TYPE_CHECKING

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from dlboard.models import ActivityStats, NewExperiment
from dlboard.serve import ClientsideScript, Icon, get_current_user, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, ModalId, ValueId
from dlboard.serve._pages._card_grid import (
    PAGE_SIZE,
    CardGrid,
    CardQuery,
    Cards,
    CardSource,
    card_grid,
    redraw_cards,
    register_card_grid,
)
from dlboard.serve._pages._comparison._cards import COMPARISON_CARDS
from dlboard.serve._pages._dash_helpers import entity_card
from dlboard.serve._pages._dataframe_helpers import experiment_display_name
from dlboard.serve._pages._delete_confirm import (
    DeleteConfirmIds,
    register_delete_callbacks,
    render_delete_control,
)
from dlboard.serve._pages._onboarding import first_experiment_snippet, onboarding_card
from dlboard.serve._pages._page_header import PageHeaderIds, page_header, register_page_header
from dlboard.serve._pages._page_sections import ProjectPageTab, project_tabs
from dlboard.serve._pages._pins._pinned_runs import register as register_pinned_runs
from dlboard.serve._pages._project_members import register_members_callbacks, render_members_section
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dash.development.base_component import Component

    from dlboard.models import DataStore, Experiment, Project


class _ProjectPage:
    """Page tag: marks a component id as belonging to `simple_project_page.py`."""


NEW_EXP_BUTTON_ID: typing.Final = "new-experiment-button"
NEW_EXP_NAME_ID: typing.Final = "new-experiment-name"
PROJECT_TABS_ID: typing.Final = ValueId[_ProjectPage]("project-tabs")
_SYNC_TAB_URL_JS = ClientsideScript(Path(__file__).with_name("sync_project_tab_url.js"))

PROJECT_HEADER_IDS = PageHeaderIds[_ProjectPage].of("project")

PROJECT_DELETE_IDS = DeleteConfirmIds(
    button=ButtonId[_ProjectPage]("delete-project-button"),
    modal=ModalId[_ProjectPage]("delete-project-modal"),
    confirm=ButtonId[_ProjectPage]("delete-project-confirm"),
    cancel=ButtonId[_ProjectPage]("delete-project-cancel"),
)


def _experiment_card(experiment: Experiment, stats: ActivityStats) -> Component:
    return entity_card(
        title=experiment_display_name(experiment),
        description=experiment.description,
        href=relative_path(f"/experiment/{experiment.id}"),
        stats=stats,
        class_name="experiment-card",
    )


def _read_experiments(store: DataStore[...], project_id: int, name_contains: str, offset: int) -> Cards:
    found = store.search_experiments(project_id, name_contains=name_contains, limit=PAGE_SIZE, offset=offset)
    stats = store.get_experiment_stats(project_id, experiment_ids=[e.id for e in found.experiments])
    return Cards(
        found.total, [_experiment_card(e, stats.get(e.id, ActivityStats())) for e in found.experiments]
    )


def _no_experiments(project_id: int, query: CardQuery) -> Component:
    """None matches the filter; or there are none yet, so how to log the first."""
    if query.q:
        return dmc.Text("No experiment matches.", c="dimmed", size="sm")
    return onboarding_card(
        title="No experiments yet — log your first run", snippet=first_experiment_snippet(project_id)
    )


EXPERIMENT_CARDS = CardSource(
    grid=CardGrid.EXPERIMENTS, label="Experiments", read=_read_experiments, empty=_no_experiments
)
"""The project page's experiments, the newest first."""


def _visible_project(project_id: int) -> Project:
    project = get_data_store().get_project(project_id)
    if project is None:
        msg = f"Project {project_id} doesn't exist, or you don't have access to it"
        raise LookupError(msg)
    return project


def render_project_page(project_id: int, query: Mapping[str, str]) -> dmc.Container:
    """The whole project page, rendered in the page's own `layout()` -- no callback round trip -- as its URL's `query` asks."""
    store = get_data_store()
    project = store.get_project(project_id)
    if project is None:
        return dmc.Container(
            dmc.Text("This project doesn't exist, or you don't have access to it.", c="dimmed"), py="xl"
        )
    members = render_members_section(project)
    tabs = [
        (
            ProjectPageTab.EXPERIMENTS,
            "Experiments",
            card_grid(EXPERIMENT_CARDS, store, project_id, CardQuery.of(CardGrid.EXPERIMENTS, query)),
        ),
        (
            ProjectPageTab.COMPARISONS,
            "Comparisons",
            card_grid(COMPARISON_CARDS, store, project_id, CardQuery.of(CardGrid.COMPARISONS, query)),
        ),
        *([(ProjectPageTab.SHARING, "Sharing", members)] if members is not None else []),
        *((tab.key, tab.label, drawn) for tab, drawn in project_tabs(project)),
    ]
    keys = [str(key) for key, _label, _drawn in tabs]
    asked = query.get("tab", "")
    return dmc.Container(
        [
            dmc.Box(
                page_header(
                    PROJECT_HEADER_IDS,
                    title=project.name,
                    description=project.description,
                    actions=render_delete_control(
                        PROJECT_DELETE_IDS, label="Delete project", entity_noun="project", icon_only=True
                    ),
                    aside=[
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=NEW_EXP_NAME_ID,
                                    placeholder="New experiment name",
                                    w=240,
                                    **typing.cast(
                                        "dict[str, typing.Any]", {"aria-label": "New experiment name"}
                                    ),
                                ),
                                dmc.Button("Create", id=NEW_EXP_BUTTON_ID, leftSection=icon(Icon.ADD)),
                            ],
                            gap="xs",
                            align="flex-start",
                            wrap="nowrap",
                        )
                    ],
                ),
                mb="lg",
            ),
            # A tab each, so what plugins add never lengthens the page, and says whose it is (#615). Every tab is drawn
            # here, in the page's own layout: switching tabs costs nothing, and a callback writing into one finds it.
            dmc.Tabs(
                [
                    dmc.TabsList([dmc.TabsTab(label, value=str(key)) for key, label, _drawn in tabs]),
                    *(dmc.TabsPanel(drawn, value=str(key), pt="md") for key, _label, drawn in tabs),
                ],
                id=PROJECT_TABS_ID,
                value=asked if asked in keys else str(ProjectPageTab.EXPERIMENTS),
            ),
        ],
        size="lg",
        py="xl",
    )


def register(app: Dash) -> None:
    """Wire the project page's create/edit/delete actions, and the sidebar of pinned runs."""
    register_card_grid(app, EXPERIMENT_CARDS)
    register_card_grid(app, COMPARISON_CARDS)
    experiments = CardGrid.EXPERIMENTS

    @app.callback(
        Output(experiments.list_id, "children", allow_duplicate=True),
        Output(experiments.count_id, "children", allow_duplicate=True),
        Output(experiments.pager_id, "total", allow_duplicate=True),
        Output(experiments.pager_id, "value", allow_duplicate=True),
        Output(experiments.pager_id, "style", allow_duplicate=True),
        Output(experiments.filter_id, "style", allow_duplicate=True),
        Output(experiments.filter_id, "value"),
        Output(NEW_EXP_NAME_ID, "value"),
        Output(NEW_EXP_NAME_ID, "error"),
        Output(PROJECT_TABS_ID, "value"),
        Input(NEW_EXP_BUTTON_ID, "n_clicks"),
        Input(NEW_EXP_NAME_ID, "n_submit"),
        State(constants.STATE_PROJECT_ID, "data"),
        State(NEW_EXP_NAME_ID, "value"),
        prevent_initial_call=True,
    )
    def create_experiment(
        n_clicks: int | None, n_submit: int | None, project_id: int, name: str | None
    ) -> tuple[typing.Any, ...]:
        if not n_clicks and not n_submit:
            raise PreventUpdate
        if not name or not name.strip():
            return *(no_update,) * 8, "Give the experiment a name", no_update
        store = get_data_store()
        store.create_experiment(
            NewExperiment(project_id=project_id, name=name.strip(), created_by=get_current_user().id)
        )
        # The new experiment is the newest: the first page, unfiltered, shows it, on the Experiments tab.
        cards = redraw_cards(EXPERIMENT_CARDS, store, project_id, CardQuery())
        return *cards, "", "", None, str(ProjectPageTab.EXPERIMENTS)

    def _fetch_project_header(project_id: int) -> tuple[str, str]:
        project = _visible_project(project_id)
        return project.name, project.description

    def _save_project_description(project_id: int, _name: str | None, description: str) -> tuple[str, str]:
        project = _visible_project(project_id)
        updated = get_data_store().update_project(project.model_copy(update={"description": description}))
        return updated.name, updated.description

    register_page_header(
        app,
        PROJECT_HEADER_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        fetch=_fetch_project_header,
        save=_save_project_description,
    )

    def _delete_project(project_id: int) -> str:
        store = get_data_store()
        store.delete_project(project_id, get_current_user())
        return relative_path("/")

    register_delete_callbacks(
        app,
        PROJECT_DELETE_IDS,
        State(constants.STATE_PROJECT_ID, "data"),
        on_confirm=_delete_project,
    )
    register_members_callbacks(app)
    register_pinned_runs(app)
    app.clientside_callback(
        _SYNC_TAB_URL_JS.source,
        Output(PROJECT_TABS_ID, "className"),
        Input(PROJECT_TABS_ID, "value"),
        prevent_initial_call=True,
    )
