"""
Auto-populate charts for a brand-new (empty) view; suggest charts for un-charted keys otherwise.

Owns the suggest-charts drawer and the auto-generate modal (both rendered once by the experiment page,
beside the panel area -- see `chart_suggestion_shells`), their ids, and the callbacks that drive both
flows. The buttons that open them ("Suggest charts", a panel's own "Suggest charts", an empty view's
"Auto-generate charts") are part of the panel area's render tree.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypedDict, cast, get_args

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import TypeAdapter

from dlboard.models import ChartInstance, ColumnKind
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon
from dlboard.serve import _constants as constants
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, PatternId, StoreId, ValueId
from dlboard.serve._pages._dash_helpers import require_triggered_id
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_data as panel_data
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._chart_autogen import (
    SplitMode,
    Suggestion,
    SuggestSort,
    build_auto_panels,
    build_suggestions,
    find_uncharted_keys,
    group_keys_into_panels,
    panel_to_open,
    parse_selection,
    visible_suggestions,
)
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from dash.development.base_component import Component


_SUGGEST_SELECTION_JS = ClientsideScript(Path(__file__).with_name("suggest_selection.js"))

_NO_KEYS_MESSAGE = "No metrics or artifacts logged for this experiment yet"

# --- suggest-charts drawer ---
SUGGEST_DRAWER_ID: ModalId[ids.ExperimentPage] = ModalId("suggest-charts-drawer")
SUGGEST_DELIMITER_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-delimiter")
SUGGEST_MODE_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-mode")
SUGGEST_CONTENT_ID: DivId[ids.ExperimentPage] = DivId("suggest-charts-content")
SUGGEST_SUGGESTIONS_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("suggest-charts-store")
SUGGEST_SCOPE_STORE_ID: StoreId[ids.ExperimentPage] = StoreId("suggest-charts-scope")
SUGGEST_FILTER_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-filter")
SUGGEST_SORT_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-sort")
SUGGEST_SELECTION_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-selection")
SUGGEST_SELECT_ALL_ID: ButtonId[ids.ExperimentPage] = ButtonId("suggest-charts-select-all")
SUGGEST_CLEAR_ID: ButtonId[ids.ExperimentPage] = ButtonId("suggest-charts-clear")
SUGGEST_ADD_SELECTED_ID: ButtonId[ids.ExperimentPage] = ButtonId("suggest-charts-add-selected")

# --- auto-generate modal, opened from an empty view's `AUTO_POPULATE_OPEN_ID` ---
AUTO_POPULATE_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("auto-populate-modal")
AUTO_POPULATE_DELIMITER_ID: ValueId[ids.ExperimentPage] = ValueId("auto-populate-delimiter")
AUTO_POPULATE_MODE_ID: ValueId[ids.ExperimentPage] = ValueId("auto-populate-mode")
AUTO_POPULATE_PREVIEW_ID: DivId[ids.ExperimentPage] = DivId("auto-populate-preview")
AUTO_POPULATE_BUTTON_ID: ButtonId[ids.ExperimentPage] = ButtonId("auto-populate-button")
AUTO_POPULATE_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("auto-populate-cancel")

_SPLIT_MODES: Final = get_args(SplitMode)
_SPLIT_MODE_DATA: Final = [{"label": mode.title(), "value": mode} for mode in _SPLIT_MODES]
_SPLIT_MODE: Final = TypeAdapter(SplitMode)
"""Checks a mode the browser sent is one the control offers, instead of quietly falling back to the first."""
_DEFAULT_DELIMITER: Final = "/"


class _SuggestionPattern(PatternId[ids.ExperimentPage]):
    """A family of components with one per suggested chart, by the kind and key of what it would show."""

    KEYS: ClassVar[tuple[str, ...]] = ("kind", "key")

    def of(self, kind: ColumnKind, key: str) -> dict[str, str]:
        return {"type": self.type, "kind": kind.value, "key": key}


_ADD_SUGGESTION: Final = _SuggestionPattern("add-suggestion")


def _split_mode_control(control_id: ValueId[ids.ExperimentPage]) -> dmc.SegmentedControl:
    return dmc.SegmentedControl(id=control_id, data=_SPLIT_MODE_DATA, value=_SPLIT_MODES[0], size="sm")


def _suggest_charts_drawer() -> dmc.Drawer:
    return dmc.Drawer(
        id=SUGGEST_DRAWER_ID,
        title="Suggested charts",
        position="right",
        size="md",
        opened=False,
        children=[
            Store(id=SUGGEST_SUGGESTIONS_STORE_ID, data=[]),
            Store(id=SUGGEST_SCOPE_STORE_ID, data=None),
            dmc.Group(
                [
                    dmc.TextInput(
                        id=SUGGEST_DELIMITER_ID, label="Delimiter", value=_DEFAULT_DELIMITER, w=90, size="sm"
                    ),
                    _split_mode_control(SUGGEST_MODE_ID),
                ],
                gap="sm",
                mb="sm",
            ),
            # Sticky, so "Add selected" stays in reach however far down a long list you have scrolled.
            dmc.Stack(
                [
                    dmc.TextInput(
                        id=SUGGEST_FILTER_ID, placeholder="Filter by name or panel", debounce=300, size="sm"
                    ),
                    dmc.Group(
                        [
                            dmc.SegmentedControl(
                                id=SUGGEST_SORT_ID,
                                data=[
                                    {"value": sort.value, "label": f"By {sort.value}"} for sort in SuggestSort
                                ],
                                value=SuggestSort.NAME.value,
                                size="xs",
                            ),
                            dmc.Button(
                                "Select all",
                                id=SUGGEST_SELECT_ALL_ID,
                                variant="default",
                                size="xs",
                                n_clicks=0,
                            ),
                            dmc.Button(
                                "Clear", id=SUGGEST_CLEAR_ID, variant="default", size="xs", n_clicks=0
                            ),
                            dmc.Button(
                                "Add selected (0)",
                                id=SUGGEST_ADD_SELECTED_ID,
                                size="xs",
                                disabled=True,
                                n_clicks=0,
                            ),
                        ],
                        gap="xs",
                    ),
                ],
                gap="xs",
                pb="sm",
                style={
                    "position": "sticky",
                    "top": 0,
                    "zIndex": 1,
                    "background": "var(--mantine-color-body)",
                },
            ),
            dmc.CheckboxGroup(id=SUGGEST_SELECTION_ID, value=[], children=html.Div(id=SUGGEST_CONTENT_ID)),
        ],
    )


def _auto_populate_modal() -> dmc.Modal:
    """The confirm modal behind an empty view's "Auto-generate charts": a preview of the panels it would make."""
    return dmc.Modal(
        id=AUTO_POPULATE_MODAL_ID,
        title="Auto-generate charts",
        size="lg",
        opened=False,
        children=dmc.Stack(
            [
                dmc.Text(
                    "Each metric and artifact name is split on the delimiter, and grouped into a "
                    "panel by its first part (prefix) or last part (suffix).",
                    size="sm",
                    c="dimmed",
                ),
                dmc.Group(
                    [
                        dmc.TextInput(
                            id=AUTO_POPULATE_DELIMITER_ID,
                            label="Delimiter",
                            value=_DEFAULT_DELIMITER,
                            w=90,
                            size="sm",
                        ),
                        dmc.Stack(
                            [
                                dmc.Text("Group by", size="sm", fw=500),
                                _split_mode_control(AUTO_POPULATE_MODE_ID),
                            ],
                            gap=4,
                        ),
                    ],
                    align="flex-end",
                ),
                html.Div(id=AUTO_POPULATE_PREVIEW_ID),
                dmc.Group(
                    [
                        dmc.Button("Cancel", id=AUTO_POPULATE_CANCEL_ID, variant="default"),
                        dmc.Button("Create charts", id=AUTO_POPULATE_BUTTON_ID, n_clicks=0),
                    ],
                    justify="flex-end",
                ),
            ]
        ),
    )


def chart_suggestion_shells() -> list[Component]:
    """The suggest-charts drawer and auto-generate modal, for the experiment page to render once, beside the panel area."""
    return [_suggest_charts_drawer(), _auto_populate_modal()]


class _AutoPopulateCtx(edits.EditCtx):
    delimiter: str | None
    mode: str | None
    experiment_id: int


def _render_suggestions(suggestions: Iterable[Suggestion], *, filtered_out_all: bool = False) -> Component:
    """
    The suggestion rows, each a checkbox (for the selection) plus a one-click add.

    `filtered_out_all` says the list is empty because of the filter, not because there is nothing left to chart.
    """

    def _row(s: Suggestion) -> Component:
        return dmc.Group(
            [
                dmc.Checkbox(
                    value=s.selection_value,
                    size="sm",
                    label=dmc.Stack(
                        [
                            dmc.Text(s.key, size="sm", ff="monospace"),
                            dmc.Text([icon(Icon.MOVE_TO), " ", s.panel_name], size="xs", c="dimmed"),
                        ],
                        gap=0,
                    ),
                ),
                dmc.ActionIcon(
                    icon(Icon.ADD),
                    id=_ADD_SUGGESTION.of(s.kind, s.key),
                    n_clicks=0,
                    variant="light",
                    size="sm",
                    **cast("dict[str, Any]", {"aria-label": f"Add a chart for {s.key}"}),
                ),
            ],
            justify="space-between",
            wrap="nowrap",
        )

    # One pass, straight into the sections it will be rendered in.
    titles = {
        ColumnKind.METRIC: "Metrics",
        ColumnKind.ARTIFACT: "Artifacts",
        ColumnKind.HISTOGRAM: "Histograms",
    }
    sections: dict[ColumnKind, list[Component]] = {kind: [] for kind in titles}
    for s in suggestions:
        sections[s.kind].append(_row(s))
    if not any(sections.values()):
        return dmc.Text(
            "No suggestion matches that filter."
            if filtered_out_all
            else "Every metric, artifact and histogram already has a chart.",
            c="dimmed",
            size="sm",
        )
    return dmc.Stack(
        [
            part
            for kind, title in titles.items()
            if sections[kind]
            for part in (dmc.Text(title, fw=600, size="sm", mt="sm"), *sections[kind])
        ],
        gap="xs",
    )


def _suggestions_in(stored: Iterable[dict[str, Any]]) -> Iterator[Suggestion]:
    """The `Suggestion`s a `SUGGEST_SUGGESTIONS_STORE_ID` payload describes, one at a time."""
    for s in stored:
        yield Suggestion(
            key=s["key"],
            kind=ColumnKind(s["kind"]),
            panel_name=s["panel_name"],
            chart=ChartInstance[Any, Any](chart_type=s["chart_type"], parameters=s["parameters"]),
        )


def _stored(suggestions: Iterable[Suggestion]) -> list[dict[str, Any]]:
    """What `SUGGEST_SUGGESTIONS_STORE_ID` holds for `suggestions` (a list: it is JSON) -- `_suggestions_in`'s input."""
    return [
        {
            "key": s.key,
            "kind": s.kind.value,
            "panel_name": s.panel_name,
            "chart_type": s.chart.chart_type,
            "parameters": s.chart.parameters,
        }
        for s in suggestions
    ]


def _shown(stored: list[dict[str, Any]], text: str | None, sort: str | None) -> list[Suggestion]:
    """The stored suggestions that pass the drawer's filter, in its sort order."""
    return visible_suggestions(
        _suggestions_in(stored), text=text or "", sort=SuggestSort(sort or SuggestSort.NAME)
    )


def _render_list(stored: list[dict[str, Any]], text: str | None, sort: str | None) -> Component:
    shown = _shown(stored, text, sort)
    return _render_suggestions(shown, filtered_out_all=bool(stored) and not shown)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


_PREVIEW_MAX_PANELS: Final = 20
"""
How many panels the preview lists. Each is a handful of Mantine components, and the browser spends
about 12 ms on each (239 panels -- grouping a Lightning run by suffix -- froze it for 3 s), while a
list that long can't be read anyway. The heading still counts every panel and chart.
"""


def _render_auto_preview(groups: dict[str, list[tuple[str, ColumnKind]]]) -> Component:
    """What "Create charts" would add: each panel (the first `_PREVIEW_MAX_PANELS`), and the keys charted in it."""
    n_charts = sum(len(keys) for keys in groups.values())
    shown = list(groups.items())[:_PREVIEW_MAX_PANELS]
    hidden = len(groups) - len(shown)
    return dmc.Stack(
        [
            dmc.Text(f"{_plural(len(groups), 'panel')} · {_plural(n_charts, 'chart')}", size="sm", fw=600),
            dmc.ScrollArea(
                dmc.Stack(
                    [
                        dmc.Paper(
                            [
                                dmc.Group(
                                    [
                                        dmc.Text(panel_name, size="sm", fw=600),
                                        dmc.Badge(_plural(len(keys), "chart"), variant="light", size="sm"),
                                    ],
                                    justify="space-between",
                                    wrap="nowrap",
                                ),
                                dmc.Text(
                                    ", ".join(key for key, _kind in keys), size="xs", c="dimmed", lineClamp=2
                                ),
                            ],
                            withBorder=True,
                            radius="sm",
                            px="sm",
                            py="xs",
                        )
                        for panel_name, keys in shown
                    ]
                    + (
                        [dmc.Text(f"…and {_plural(hidden, 'more panel')}", size="xs", c="dimmed")]
                        if hidden
                        else []
                    ),
                    gap="xs",
                ),
                mah=320,
                type="auto",
            ),
        ],
        gap="xs",
    )


def _register_auto_populate(app: Dash) -> None:
    @app.callback(
        Output(AUTO_POPULATE_MODAL_ID, "opened"),
        Output(AUTO_POPULATE_PREVIEW_ID, "children"),
        Output(AUTO_POPULATE_BUTTON_ID, "disabled"),
        # Optional: only an empty view offers it, while the modal is always on the page.
        Input(panel_view.AUTO_POPULATE_OPEN_ID, "n_clicks", allow_optional=True),
        Input(AUTO_POPULATE_DELIMITER_ID, "value"),
        Input(AUTO_POPULATE_MODE_ID, "value"),
        State(AUTO_POPULATE_MODAL_ID, "opened"),
        State(constants.STATE_EXPERIMENT_ID, "data"),
        prevent_initial_call=True,
    )
    def preview_auto_populate(
        n_clicks: int | None,
        delimiter: str | None,
        mode: str | None,
        opened: bool,  # noqa: FBT001
        experiment_id: int,
    ) -> tuple[bool | NoUpdate, Component, bool]:
        """Open the confirm modal, and keep its preview in step with the delimiter and grouping picked."""
        opening = cast("str | None", ctx.triggered_id) == panel_view.AUTO_POPULATE_OPEN_ID
        if (opening and not n_clicks) or (not opening and not opened):
            raise PreventUpdate
        store = get_data_store()
        catalog = ColumnCatalog.load(store, experiment_id)
        if not catalog.has_chartable_keys:
            return True, dmc.Text(_NO_KEYS_MESSAGE, c="dimmed", size="sm"), True
        groups = group_keys_into_panels(
            catalog,
            delimiter=delimiter or _DEFAULT_DELIMITER,
            mode=_SPLIT_MODE.validate_python(mode),
            lightning=panel_data.is_lightning_experiment(store, experiment_id),
        )
        return True if opening else no_update, _render_auto_preview(groups), False

    @app.callback(
        Output(AUTO_POPULATE_MODAL_ID, "opened", allow_duplicate=True),
        Input(AUTO_POPULATE_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_auto_populate(n_clicks: int | None) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False

    # The modal is always on the page (`chart_suggestion_shells`), not inside the empty view this
    # replaces, so creating the charts closes it here rather than by unmounting it.
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(AUTO_POPULATE_MODAL_ID, "opened", allow_duplicate=True),
        Input(AUTO_POPULATE_BUTTON_ID, "n_clicks"),
        {
            "delimiter": State(AUTO_POPULATE_DELIMITER_ID, "value"),
            "mode": State(AUTO_POPULATE_MODE_ID, "value"),
            "experiment_id": State(constants.STATE_EXPERIMENT_ID, "data"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "paging_json": State(ids.PANEL_PAGING_ID, "data"),
        },
        prevent_initial_call=True,
    )
    def auto_populate_charts(n_clicks: int, auto_populate_ctx: _AutoPopulateCtx) -> tuple[Any, str, bool]:
        if not n_clicks:
            raise PreventUpdate
        experiment_id = auto_populate_ctx["experiment_id"]
        store = get_data_store()
        catalog = ColumnCatalog.load(store, experiment_id)
        if not catalog.has_chartable_keys:
            raise ValueError(_NO_KEYS_MESSAGE)
        split_mode = _SPLIT_MODE.validate_python(auto_populate_ctx["mode"])
        lightning = panel_data.is_lightning_experiment(store, experiment_id)

        panels = build_auto_panels(
            catalog,
            delimiter=auto_populate_ctx["delimiter"] or _DEFAULT_DELIMITER,
            mode=split_mode,
            lightning=lightning,
        )
        page, container = edits.commit(
            auto_populate_ctx["page_json"],
            auto_populate_ctx["paging_json"],
            edits.PageEdit(
                panels=lambda _panels: panels,
                open_panels=panel_to_open(panels),
            ),
        )
        return container, page.model_dump_json(), False


def _resolve_suggestion_trigger(
    n_clicks: int, current_scope: str | None
) -> tuple[str | None, bool | NoUpdate]:
    """
    The suggestion scope (`None` = whole experiment) and whether to open the drawer.

    Raises `PreventUpdate` for a freshly-mounted button's own spurious "triggered" report (see
    `refresh_suggestions`); otherwise falls back to `current_scope`/leaving `opened` alone for any
    other trigger (the drawer's own delimiter/mode inputs, which only need suggestions recomputed).
    """
    triggered_id = cast(
        "str | dict[str, str] | None",
        ctx.triggered_id,
    )
    if not triggered_id:
        raise PreventUpdate
    if triggered_id == panel_view.SUGGEST_CHARTS_BUTTON_ID:
        if not n_clicks:
            raise PreventUpdate
        return None, True
    if isinstance(triggered_id, dict) and triggered_id.get("type") == panel_view.PANEL_SUGGEST_BUTTON.type:
        if not ctx.triggered[0]["value"]:  # pyrefly: ignore [unsupported-operation]
            raise PreventUpdate
        return triggered_id["panel"], True
    return current_scope, no_update


class _SuggestCtx(TypedDict):
    delimiter: str | None
    mode: str | None
    page_json: str
    current_scope: str | None
    filter_text: str | None
    sort: str | None


def _add_charts(
    page_json: str,
    paging_json: str,
    stored: list[dict[str, Any]],
    picked: frozenset[tuple[ColumnKind, str]],
) -> tuple[Any, str, list[dict[str, Any]]]:
    """
    Add the chart of every stored suggestion in `picked` to its panel, in one page update.

    Returns the re-rendered page content, the saved page, and the suggestions still left. However
    many are picked, the page is saved and rebuilt once -- adding them one click at a time did that
    per chart.
    """
    chosen = (s for s in _suggestions_in(stored) if (s.kind, s.key) in picked)
    first = next(chosen, None)
    if first is None:
        raise PreventUpdate

    def add_all(panels: list[Any]) -> list[Any]:
        for s in itertools.chain([first], chosen):
            panels = page_model.add_chart_to_panel_by_name(panels, s.panel_name, s.chart)
        return panels

    page, container = edits.commit(page_json, paging_json, edits.PageEdit(panels=add_all))
    remaining = [e for e in stored if (ColumnKind(e["kind"]), e["key"]) not in picked]
    return container, page.model_dump_json(), remaining


def _register_suggestions(app: Dash) -> None:
    @app.callback(
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "title", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Output(SUGGEST_SCOPE_STORE_ID, "data", allow_duplicate=True),
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_FILTER_ID, "value", allow_duplicate=True),
        # Optional: an empty view's toolbar offers "Auto-generate charts" instead, while the drawer
        # (`chart_suggestion_shells`) is always on the page.
        Input(panel_view.SUGGEST_CHARTS_BUTTON_ID, "n_clicks", allow_optional=True),
        Input(panel_view.PANEL_SUGGEST_BUTTON.all(), "n_clicks"),
        {
            "delimiter": Input(SUGGEST_DELIMITER_ID, "value"),
            "mode": Input(SUGGEST_MODE_ID, "value"),
            "page_json": State(ids.STATE_PAGE_STORAGE, "data"),
            "current_scope": State(SUGGEST_SCOPE_STORE_ID, "data"),
            "filter_text": State(SUGGEST_FILTER_ID, "value"),
            "sort": State(SUGGEST_SORT_ID, "value"),
        },
        prevent_initial_call=True,
    )
    def refresh_suggestions(
        n_clicks: int, _panel_clicks: list[int], suggest_ctx: _SuggestCtx
    ) -> tuple[
        bool | NoUpdate,
        str | NoUpdate,
        Component,
        list[dict[str, Any]],
        str | None,
        list[str],
        str | NoUpdate,
    ]:
        # Either button mounting for the first time reports itself as "triggered" with `n_clicks`
        # still 0 -- each is rendered into its own container (the panel controls row / a panel's
        # own `panel_header_controls`), separate from the delimiter/mode inputs below, so
        # `ctx.triggered_id` unambiguously resolves to the button on that mount even though
        # nothing was clicked.
        scope, opened = _resolve_suggestion_trigger(n_clicks, suggest_ctx["current_scope"])
        # The drawer outlives each opening, filter box included: a fresh opening starts unfiltered,
        # while regrouping (delimiter/mode) keeps whatever filter is typed.
        filter_text, filter_value = ("", "") if opened is True else (suggest_ctx["filter_text"], no_update)

        curr_page = page_model.BasicExperimentPage.model_validate_json(suggest_ctx["page_json"])
        store = get_data_store()
        catalog = (
            ColumnCatalog.load(store, curr_page.experiment_id)
            if curr_page.experiment_id is not None
            else ColumnCatalog()
        )
        title = f"Suggested charts for {scope}" if scope else "Suggested charts"
        if not catalog.has_chartable_keys:
            return (
                opened,
                title,
                dmc.Text(_NO_KEYS_MESSAGE, c="dimmed", size="sm"),
                [],
                scope,
                [],
                filter_value,
            )

        split_mode = _SPLIT_MODE.validate_python(suggest_ctx["mode"])
        lightning = curr_page.experiment_id is not None and panel_data.is_lightning_experiment(
            store, curr_page.experiment_id
        )
        suggestions = build_suggestions(
            find_uncharted_keys(curr_page.panels, catalog),
            catalog,
            delimiter=suggest_ctx["delimiter"] or _DEFAULT_DELIMITER,
            mode=split_mode,
            lightning=lightning,
        )
        if scope is not None:
            suggestions = [s for s in suggestions if s.panel_name == scope]

        stored = _stored(suggestions)
        # A different scope, or a regrouping, is a different list: whatever was ticked no longer applies.
        return (
            opened,
            title,
            _render_list(stored, filter_text, suggest_ctx["sort"]),
            stored,
            scope,
            [],
            filter_value,
        )

    @app.callback(
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Input(SUGGEST_FILTER_ID, "value"),
        Input(SUGGEST_SORT_ID, "value"),
        State(SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def filter_and_sort_suggestions(
        filter_text: str | None, sort: str | None, stored: list[dict[str, Any]] | None
    ) -> Component:
        """Re-list what is already loaded; the ticked boxes live in the checkbox group, so they survive."""
        return _render_list(stored or [], filter_text, sort)

    @app.callback(
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Input(SUGGEST_SELECT_ALL_ID, "n_clicks"),
        Input(SUGGEST_CLEAR_ID, "n_clicks"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        State(SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(SUGGEST_SELECTION_ID, "value"),
        prevent_initial_call=True,
    )
    def select_suggestions(  # noqa: PLR0913
        select_all_clicks: int | None,
        clear_clicks: int | None,
        filter_text: str | None,
        sort: str | None,
        stored: list[dict[str, Any]] | None,
        selected: list[str] | None,
    ) -> list[str]:
        """Tick every suggestion the filter shows (keeping what was already ticked), or untick everything."""
        if cast("str | None", ctx.triggered_id) == SUGGEST_CLEAR_ID:
            if not clear_clicks:
                raise PreventUpdate
            return []
        if not select_all_clicks:
            raise PreventUpdate
        shown = [s.selection_value for s in _shown(stored or [], filter_text, sort)]
        return [*dict.fromkeys([*(selected or []), *shown])]

    app.clientside_callback(
        _SUGGEST_SELECTION_JS.source,
        Output(SUGGEST_ADD_SELECTED_ID, "children"),
        Output(SUGGEST_ADD_SELECTED_ID, "disabled"),
        Input(SUGGEST_SELECTION_ID, "value"),
    )


def _register_suggestion_adds(app: Dash) -> None:
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Input(_ADD_SUGGESTION.all(), "n_clicks"),
        State(SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        State(SUGGEST_SELECTION_ID, "value"),
        prevent_initial_call=True,
    )
    def add_suggested_chart(  # noqa: PLR0913
        _n_clicks_list: list[int],
        stored: list[dict[str, Any]] | None,
        page_json: str,
        paging_json: str,
        filter_text: str | None,
        sort: str | None,
        selected: list[str] | None,
    ) -> tuple[Any, str, Component, list[dict[str, Any]], list[str], bool]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        picked = frozenset({(ColumnKind(triggered_id["kind"]), triggered_id["key"])})
        container, saved_page, remaining = _add_charts(page_json, paging_json, stored or [], picked)
        still_ticked = [v for v in selected or [] if parse_selection([v]) != picked]
        # Closes the drawer, so the chart just added is in view.
        return (
            container,
            saved_page,
            _render_list(remaining, filter_text, sort),
            remaining,
            still_ticked,
            False,
        )

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SUGGESTIONS_STORE_ID, "data", allow_duplicate=True),
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Input(SUGGEST_ADD_SELECTED_ID, "n_clicks"),
        State(SUGGEST_SELECTION_ID, "value"),
        State(SUGGEST_SUGGESTIONS_STORE_ID, "data"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        prevent_initial_call=True,
    )
    def add_selected_suggestions(  # noqa: PLR0913
        n_clicks: int | None,
        selected: list[str] | None,
        stored: list[dict[str, Any]] | None,
        page_json: str,
        paging_json: str,
        filter_text: str | None,
        sort: str | None,
    ) -> tuple[Any, str, Component, list[dict[str, Any]], list[str], bool]:
        """Add every ticked suggestion's chart at once, wherever the current filter has scrolled them."""
        if not n_clicks:
            raise PreventUpdate
        container, saved_page, remaining = _add_charts(
            page_json, paging_json, stored or [], parse_selection(selected or [])
        )
        return container, saved_page, _render_list(remaining, filter_text, sort), remaining, [], False


def register_chart_suggestions_callbacks(app: Dash) -> None:
    _register_auto_populate(app)
    _register_suggestions(app)
    _register_suggestion_adds(app)
