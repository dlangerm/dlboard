"""
Auto-populate charts for a brand-new (empty) view; suggest charts for un-charted keys otherwise.

Owns the suggest-charts drawer and the auto-generate modal (both rendered once by the experiment page,
beside the panel area -- see `chart_suggestion_shells`), their ids, and the callbacks that drive both
flows. The buttons that open them ("Suggest charts", a panel's own "Suggest charts", an empty view's
"Auto-generate charts") are part of the panel area's render tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypedDict, cast, get_args

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, ctx, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import TypeAdapter

from dlboard.models import ColumnKind
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
    UnchartedKeys,
    build_auto_panels,
    build_suggestions,
    find_uncharted_keys,
    group_keys_into_panels,
    panel_to_open,
    parse_selection,
    visible_suggestions,
)
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog, single_value_metrics

if TYPE_CHECKING:
    from dash.development.base_component import Component


_SUGGEST_SELECTION_JS = ClientsideScript(Path(__file__).with_name("suggest_selection.js"))

_NO_KEYS_MESSAGE = "No metrics or artifacts logged for this experiment yet"

# --- suggest-charts drawer ---
SUGGEST_DRAWER_ID: ModalId[ids.ExperimentPage] = ModalId("suggest-charts-drawer")
SUGGEST_DELIMITER_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-delimiter")
SUGGEST_MODE_ID: ValueId[ids.ExperimentPage] = ValueId("suggest-charts-mode")
SUGGEST_CONTENT_ID: DivId[ids.ExperimentPage] = DivId("suggest-charts-content")
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
_SUGGESTIONS_SHOWN: Final = 50
"""How many suggestions the drawer lists at once: a filter narrows the rest, so the browser never mounts every row."""


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


def _render_suggestions(
    suggestions: list[Suggestion], *, total: int, filtered_out_all: bool = False
) -> Component:
    """
    The suggestion rows, each a checkbox (for the selection) plus a one-click add.

    `suggestions` is the first page of the `total` the filter leaves. `filtered_out_all` says the list is empty
    because of the filter, not because there is nothing left to chart.
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
    more = [dmc.Text(f"Showing {len(suggestions)} of {total}. Filter to narrow.", c="dimmed", size="sm")]
    return dmc.Stack(
        [
            part
            for kind, title in titles.items()
            if sections[kind]
            for part in (dmc.Text(title, fw=600, size="sm", mt="sm"), *sections[kind])
        ]
        + (more if total > len(suggestions) else []),
        gap="xs",
    )


def _listed(
    page_json: str, delimiter: str | None, mode: str | None, scope: str | None
) -> tuple[ColumnCatalog, list[Suggestion]]:
    """
    Every suggestion the page leaves uncharted, in `scope` if there is one, with the catalog they came from.

    Built for each request rather than kept in the browser, which would otherwise hold a list that grows with every
    metric. A listed suggestion needs only its key and its panel, so its chart is built without judging each key's
    shape: `_suggestions_to_add` gives the ones actually added their real chart.
    """
    curr_page = page_model.BasicExperimentPage.model_validate_json(page_json)
    store = get_data_store()
    experiment_id = curr_page.experiment_id
    catalog = ColumnCatalog.load(store, experiment_id) if experiment_id is not None else ColumnCatalog()
    suggestions = build_suggestions(
        find_uncharted_keys(curr_page.panels, catalog),
        catalog,
        delimiter=delimiter or _DEFAULT_DELIMITER,
        mode=_SPLIT_MODE.validate_python(mode),
        lightning=experiment_id is not None and panel_data.is_lightning_experiment(store, experiment_id),
    )
    if scope is not None:
        suggestions = [s for s in suggestions if s.panel_name == scope]
    return catalog, suggestions


def _matching(suggestions: list[Suggestion], text: str | None, sort: str | None) -> list[Suggestion]:
    return visible_suggestions(suggestions, text=text or "", sort=SuggestSort(sort or SuggestSort.NAME))


def _listing(suggestions: list[Suggestion], text: str | None, sort: str | None) -> Component:
    """The drawer's list: the suggestions the filter and sort leave, the first page of them at most."""
    matching = _matching(suggestions, text, sort)
    return _render_suggestions(
        matching[:_SUGGESTIONS_SHOWN],
        total=len(matching),
        filtered_out_all=bool(suggestions) and not matching,
    )


def _suggestions_to_add(
    page_json: str, delimiter: str | None, mode: str | None, picked: frozenset[tuple[ColumnKind, str]]
) -> list[Suggestion]:
    """
    The suggestions `picked` names, with the charts they would be added as.

    A metric's chart depends on its shape, so only the picked metrics are judged. Artifacts and histograms are built
    for every uncharted one, since an artifact's chart depends on the artifacts beside it.
    """
    curr_page = page_model.BasicExperimentPage.model_validate_json(page_json)
    experiment_id = curr_page.experiment_id
    if experiment_id is None:
        return []
    store = get_data_store()
    catalog = ColumnCatalog.load(store, experiment_id)
    uncharted = find_uncharted_keys(curr_page.panels, catalog)
    metrics = [key for kind, key in picked if kind is ColumnKind.METRIC]
    suggestions = build_suggestions(
        UnchartedKeys(metrics=metrics, artifacts=uncharted.artifacts, histograms=uncharted.histograms),
        catalog,
        single_value=single_value_metrics(store, experiment_id, metrics),
        delimiter=delimiter or _DEFAULT_DELIMITER,
        mode=_SPLIT_MODE.validate_python(mode),
        lightning=panel_data.is_lightning_experiment(store, experiment_id),
    )
    return [s for s in suggestions if (s.kind, s.key) in picked]


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
            single_value=single_value_metrics(store, experiment_id, catalog.metrics),
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


def _add_charts(page_json: str, paging_json: str, chosen: list[Suggestion]) -> tuple[Any, str]:
    """
    Add the chart of every suggestion in `chosen` to its panel, in one page update.

    Returns the re-rendered page content and the saved page. However many are chosen, the page is saved and rebuilt
    once -- adding them one click at a time did that per chart.
    """
    if not chosen:
        raise PreventUpdate

    def add_all(panels: list[Any]) -> list[Any]:
        for s in chosen:
            panels = page_model.add_chart_to_panel_by_name(panels, s.panel_name, s.chart)
        return panels

    page, container = edits.commit(page_json, paging_json, edits.PageEdit(panels=add_all))
    return container, page.model_dump_json()


def _register_suggestions(app: Dash) -> None:
    @app.callback(
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "title", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SCOPE_STORE_ID, "data", allow_duplicate=True),
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_FILTER_ID, "value", allow_duplicate=True),
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
    ) -> tuple[bool | NoUpdate, str | NoUpdate, Component, str | None, list[str], str | NoUpdate]:
        # Either button mounting for the first time reports itself as "triggered" with `n_clicks`
        # still 0 -- each is rendered into its own container (the panel controls row / a panel's
        # own `panel_header_controls`), separate from the delimiter/mode inputs below, so
        # `ctx.triggered_id` unambiguously resolves to the button on that mount even though
        # nothing was clicked.
        scope, opened = _resolve_suggestion_trigger(n_clicks, suggest_ctx["current_scope"])
        # The drawer outlives each opening, filter box included: a fresh opening starts unfiltered,
        # while regrouping (delimiter/mode) keeps whatever filter is typed.
        filter_text, filter_value = ("", "") if opened is True else (suggest_ctx["filter_text"], no_update)

        title = f"Suggested charts for {scope}" if scope else "Suggested charts"
        catalog, suggestions = _listed(
            suggest_ctx["page_json"], suggest_ctx["delimiter"], suggest_ctx["mode"], scope
        )
        content: Component = (
            _listing(suggestions, filter_text, suggest_ctx["sort"])
            if catalog.has_chartable_keys
            else dmc.Text(_NO_KEYS_MESSAGE, c="dimmed", size="sm")
        )
        # A different scope, or a regrouping, is a different list: whatever was ticked no longer applies.
        return opened, title, content, scope, [], filter_value

    @app.callback(
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Input(SUGGEST_FILTER_ID, "value"),
        Input(SUGGEST_SORT_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(SUGGEST_DELIMITER_ID, "value"),
        State(SUGGEST_MODE_ID, "value"),
        State(SUGGEST_SCOPE_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def filter_and_sort_suggestions(  # noqa: PLR0913
        filter_text: str | None,
        sort: str | None,
        page_json: str,
        delimiter: str | None,
        mode: str | None,
        scope: str | None,
    ) -> Component:
        """Re-list the uncharted keys the filter leaves; the ticked boxes live in the checkbox group, so they survive."""
        _catalog, suggestions = _listed(page_json, delimiter, mode, scope)
        return _listing(suggestions, filter_text, sort)

    @app.callback(
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Input(SUGGEST_SELECT_ALL_ID, "n_clicks"),
        Input(SUGGEST_CLEAR_ID, "n_clicks"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(SUGGEST_DELIMITER_ID, "value"),
        State(SUGGEST_MODE_ID, "value"),
        State(SUGGEST_SCOPE_STORE_ID, "data"),
        State(SUGGEST_SELECTION_ID, "value"),
        prevent_initial_call=True,
    )
    def select_suggestions(  # noqa: PLR0913
        select_all_clicks: int | None,
        clear_clicks: int | None,
        filter_text: str | None,
        sort: str | None,
        page_json: str,
        delimiter: str | None,
        mode: str | None,
        scope: str | None,
        selected: list[str] | None,
    ) -> list[str]:
        """Tick every suggestion the filter leaves (not only the page shown), keeping what was ticked; or untick all."""
        if cast("str | None", ctx.triggered_id) == SUGGEST_CLEAR_ID:
            if not clear_clicks:
                raise PreventUpdate
            return []
        if not select_all_clicks:
            raise PreventUpdate
        _catalog, suggestions = _listed(page_json, delimiter, mode, scope)
        shown = [s.selection_value for s in _matching(suggestions, filter_text, sort)]
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
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Input(_ADD_SUGGESTION.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        State(SUGGEST_DELIMITER_ID, "value"),
        State(SUGGEST_MODE_ID, "value"),
        State(SUGGEST_SCOPE_STORE_ID, "data"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        State(SUGGEST_SELECTION_ID, "value"),
        prevent_initial_call=True,
    )
    def add_suggested_chart(  # noqa: PLR0913
        _n_clicks_list: list[int],
        page_json: str,
        paging_json: str,
        delimiter: str | None,
        mode: str | None,
        scope: str | None,
        filter_text: str | None,
        sort: str | None,
        selected: list[str] | None,
    ) -> tuple[Any, str, Component, list[str], bool]:
        triggered_id = cast("dict[str, str]", require_triggered_id())
        picked = frozenset({(ColumnKind(triggered_id["kind"]), triggered_id["key"])})
        container, saved_page = _add_charts(
            page_json, paging_json, _suggestions_to_add(page_json, delimiter, mode, picked)
        )
        _catalog, suggestions = _listed(saved_page, delimiter, mode, scope)
        still_ticked = [v for v in selected or [] if parse_selection([v]) != picked]
        # Closes the drawer, so the chart just added is in view.
        return container, saved_page, _listing(suggestions, filter_text, sort), still_ticked, False

    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(SUGGEST_CONTENT_ID, "children", allow_duplicate=True),
        Output(SUGGEST_SELECTION_ID, "value", allow_duplicate=True),
        Output(SUGGEST_DRAWER_ID, "opened", allow_duplicate=True),
        Input(SUGGEST_ADD_SELECTED_ID, "n_clicks"),
        State(SUGGEST_SELECTION_ID, "value"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        State(SUGGEST_DELIMITER_ID, "value"),
        State(SUGGEST_MODE_ID, "value"),
        State(SUGGEST_SCOPE_STORE_ID, "data"),
        State(SUGGEST_FILTER_ID, "value"),
        State(SUGGEST_SORT_ID, "value"),
        prevent_initial_call=True,
    )
    def add_selected_suggestions(  # noqa: PLR0913
        n_clicks: int | None,
        selected: list[str] | None,
        page_json: str,
        paging_json: str,
        delimiter: str | None,
        mode: str | None,
        scope: str | None,
        filter_text: str | None,
        sort: str | None,
    ) -> tuple[Any, str, Component, list[str], bool]:
        """Add every ticked suggestion's chart at once, wherever the current filter has scrolled them."""
        if not n_clicks:
            raise PreventUpdate
        picked = parse_selection(selected or [])
        container, saved_page = _add_charts(
            page_json, paging_json, _suggestions_to_add(page_json, delimiter, mode, picked)
        )
        _catalog, suggestions = _listed(saved_page, delimiter, mode, scope)
        return container, saved_page, _listing(suggestions, filter_text, sort), [], False


def register_chart_suggestions_callbacks(app: Dash) -> None:
    _register_auto_populate(app)
    _register_suggestions(app)
    _register_suggestion_adds(app)
