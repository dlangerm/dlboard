"""
A panel's or a page's chart settings: what every chart of it is drawn with, over each chart's own; and locking a chart.

"Chart settings…" in a panel's menu, and "Chart settings" in the page's toolbar, open one form for the charts it reaches:
a field for each setting that any of their types lets a panel or a page set (`ParameterField.layerable`), so a chart type
added to the page brings its own. A field is set (its box ticked) or left to each chart.

With no filter typed, the form is the panel's or the page's own chart settings (`PanelInstance.chart_settings`,
`DocumentSettings.chart_settings`): drawn over every chart's own (`BasicExperimentPage.drawn`), a panel's over the page's,
and never saved into a chart. A setting that no chart of it has now is kept, unshown, for one added later: the form only
writes what it shows. With the panel's chart filter (or the page's panel filter) typed, the form writes what is ticked
into each chart it matches, once. A locked chart (its lock, on its card) is drawn with its own settings, and neither
reaches it.

Each is an edit of the page like any other (`PageEdit`): on a page that isn't yours, it goes into a view of your own.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

import dash_mantine_components as dmc
from dash import Dash, Input, NoUpdate, Output, State, html, no_update
from dash.dcc import Store
from dash.exceptions import PreventUpdate
from pydantic import BaseModel

from dlboard import models
from dlboard.serve import ClientsideScript, Icon, get_data_store, icon
from dlboard.serve._component_ids import ButtonId, DivId, ModalId, StoreId
from dlboard.serve._pages._dash_helpers import busy_while_running, notice, require_triggered_id
from dlboard.serve._pages._experiment import _chart_view as chart_view
from dlboard.serve._pages._experiment import _ids as ids
from dlboard.serve._pages._experiment import _page_edits as edits
from dlboard.serve._pages._experiment import _page_model as page_model
from dlboard.serve._pages._experiment import _panel_view as panel_view
from dlboard.serve._pages._experiment._chart_settings_form import (
    FieldPattern,
    merge_chart_param_values,
    setting_input,
)
from dlboard.serve._pages._experiment._column_catalog import ColumnCatalog
from dlboard.serve._pages._experiment._page_repository import load_page, ref_of
from dlboard.serve._pages._experiment._paging import Paging, chart_matches, fuzzy_match
from dlboard.serve._pages._experiment._views import VIEW_NOTIFY_ID

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from dash.development.base_component import Component

SETTINGS_MODAL_ID: ModalId[ids.ExperimentPage] = ModalId("chart-settings-modal")
SETTINGS_TARGET_ID: StoreId[ids.ExperimentPage] = StoreId("chart-settings-target")
SETTINGS_BODY_ID: DivId[ids.ExperimentPage] = DivId("chart-settings-body")
SETTINGS_FORM_ID: DivId[ids.ExperimentPage] = DivId("chart-settings-form")
SETTINGS_APPLY_ID: ButtonId[ids.ExperimentPage] = ButtonId("chart-settings-apply")
SETTINGS_CANCEL_ID: ButtonId[ids.ExperimentPage] = ButtonId("chart-settings-cancel")

LAYER_PARAM: Final = FieldPattern("layer-param")
"""A setting's input in the form, as the chart editor's `CHART_PARAM` is: they may both be open."""
LAYER_SET: Final = FieldPattern("layer-set")
"""Whether the form sets a setting (its box), or leaves it to each chart."""

_SET_ON_CHANGE_JS: Final = ClientsideScript(Path(__file__).with_name("layer_set_on_change.js"))


class SettingsTarget(BaseModel, frozen=True, extra="forbid"):
    """What the form is for: a panel's charts (`panel`), or the page's (`None`), and the filter typed when it was opened."""

    panel: str | None
    query: str
    """The panel's chart filter, or the page's panel filter. Empty: the form is the chart settings; else it writes into what it matches."""
    tab: str
    """The tab shown when it was opened: a page's panel filter is of its panels."""


class _ApplyCtx(edits.EditCtx):
    target: Annotated[SettingsTarget | None, State(SETTINGS_TARGET_ID, "data")]
    set_ids: Annotated[list[dict[str, str]], State(LAYER_SET.all(), "id")]
    set_checked: Annotated[list[bool | None], State(LAYER_SET.all(), "checked")]
    field_ids: Annotated[list[dict[str, str]], State(LAYER_PARAM.all(), "id")]
    values: Annotated[list[Any], State(LAYER_PARAM.all(), "value")]
    checked_values: Annotated[list[Any], State(LAYER_PARAM.all(), "checked")]


def chart_settings_modal() -> dmc.Modal:
    """The form, for the page to render once, beside the panel area."""
    return dmc.Modal(
        id=SETTINGS_MODAL_ID,
        title=dmc.Text("Chart settings", fw=600),
        size=680,
        opened=False,
        children=[
            Store(id=SETTINGS_TARGET_ID),
            dmc.Stack(
                [
                    html.Div(id=SETTINGS_BODY_ID),
                    html.Div(id=SETTINGS_FORM_ID),
                    dmc.Group(
                        [
                            dmc.Button("Cancel", id=SETTINGS_CANCEL_ID, variant="default"),
                            dmc.Button("Apply", id=SETTINGS_APPLY_ID),
                        ],
                        justify="flex-end",
                    ),
                ]
            ),
        ],
    )


def layerable_fields(chart_types: Iterable[str]) -> dict[str, models.ParameterField]:
    """
    Every setting that a panel or a page may set of any of `chart_types`, once by name: the form's fields.

    Two types that name a setting alike share its field, offering what either takes: a value a chart's type does not
    take is left out of that chart (`ChartInstance.drawn_with`). A type not installed has none.
    """
    union: dict[str, models.ParameterField] = {}
    for chart_type in sorted(set(chart_types)):
        try:
            fields = models.ChartTypeRegistry.parameter_fields(chart_type)
        except models.UnknownChartTypeError:
            continue
        for name, field in fields.items():
            if field.layerable:
                union[name] = field if name not in union else _either(union[name], field)
    return union


def _either(first: models.ParameterField, other: models.ParameterField) -> models.ParameterField:
    """`first`, offering `other`'s choices and range too: the one field of a setting two chart types name alike."""
    if first.type != other.type:
        return first
    low = None if first.minimum is None or other.minimum is None else min(first.minimum, other.minimum)
    high = None if first.maximum is None or other.maximum is None else max(first.maximum, other.maximum)
    choices = (
        None
        if first.choices is None or other.choices is None
        else tuple(dict.fromkeys((*first.choices, *other.choices)))
    )
    slider = first.slider_step if low is not None and high is not None else None
    return first.model_copy(
        update={"minimum": low, "maximum": high, "choices": choices, "slider_step": slider}
    )


def layered(
    current: Mapping[str, models.SettingValue],
    shown: Iterable[str],
    chosen: Mapping[str, models.SettingValue],
) -> dict[str, models.SettingValue]:
    """
    Chart settings `current` after the form: each setting it showed is what was `chosen`, or not set if not ticked.

    A setting it did not show (no chart in reach has it now) is kept, for a chart that has it to be drawn with later.
    """
    unshown = {name: value for name, value in current.items() if name not in set(shown)}
    return {**unshown, **chosen}


def _reaches(target: SettingsTarget, panel: models.PanelInstance[Any, Any]) -> bool:
    """Whether the form reaches `panel`'s charts."""
    if target.panel is not None:
        return panel.name == target.panel
    return not target.query or (panel.tab == target.tab and fuzzy_match(target.query, panel.name))


def _reached(
    page: page_model.BasicExperimentPage, target: SettingsTarget
) -> list[models.ChartInstance[Any, Any]]:
    """The charts the form is for: with a chart filter typed, those of the panel it matches."""
    query = target.query if target.panel is not None else ""
    return [
        chart
        for panel in page.panels
        if _reaches(target, panel)
        for chart in panel.charts
        if chart_matches(chart, query)
    ]


def _current(
    page: page_model.BasicExperimentPage, target: SettingsTarget
) -> Mapping[str, models.SettingValue]:
    """The chart settings the form is of, as stored; none when it writes into charts instead."""
    if target.query:
        return {}
    if target.panel is None:
        return page.document.chart_settings
    return next((p.chart_settings for p in page.panels if p.name == target.panel), {})


def _charts(count: int) -> str:
    return "1 chart" if count == 1 else f"{count} charts"


def _kind(chart_type: str) -> str:
    """A chart type as the form names it: `image_series` is "image series"."""
    return chart_type.replace("_", " ")


def _has(chart: models.ChartInstance[Any, Any], name: str) -> bool:
    try:
        return name in models.ChartTypeRegistry.parameter_fields(chart.chart_type)
    except models.UnknownChartTypeError:
        return False


def _applies(name: str, unlocked: list[models.ChartInstance[Any, Any]]) -> str | None:
    """Which of the charts a setting reaches, under its name, if not all of them: how many, and of which types."""
    having = [chart for chart in unlocked if _has(chart, name)]
    if len(having) == len(unlocked):
        return None
    kinds = ", ".join(sorted({_kind(chart.chart_type) for chart in having}))
    return f"Only {len(having)} of {_charts(len(unlocked))}" + (f": {kinds}" if kinds else "")


def _row(
    name: str,
    field: models.ParameterField,
    current: Mapping[str, models.SettingValue],
    catalog: ColumnCatalog,
    applies: str | None,
) -> html.Div:
    """A setting's row: whether the form sets it (it does if `current` does), what it is and which charts it reaches, and its value."""
    about: list[Component] = (
        [
            dmc.Tooltip(
                html.Span(
                    icon(Icon.INFO),
                    className="dl-chart-settings-info",
                    tabIndex="0",
                    **cast("dict[str, Any]", {"aria-label": field.description, "role": "img"}),
                ),
                label=field.description,
                multiline=True,
                w=300,
                withArrow=True,
            )
        ]
        if field.description
        else []
    )
    return html.Div(
        [
            dmc.Checkbox(
                id=LAYER_SET.of(name),
                checked=name in current,
                className="dl-chart-settings-set",
                **cast("dict[str, Any]", {"aria-label": f"Set {field.label}"}),
            ),
            html.Div(
                [
                    dmc.Group([dmc.Text(field.label, size="sm", fw=500), *about], gap=4, wrap="nowrap"),
                    *([dmc.Text(applies, size="xs", c="dimmed")] if applies else []),
                ]
            ),
            html.Div(
                setting_input(
                    name, field, catalog, override=current.get(name), pattern=LAYER_PARAM, labelled=False
                ),
                className="dl-chart-settings-value",
            ),
        ],
        className="dl-chart-settings-row",
    )


def _form(
    fields: dict[str, models.ParameterField],
    current: Mapping[str, models.SettingValue],
    catalog: ColumnCatalog,
    unlocked: list[models.ChartInstance[Any, Any]],
) -> list[Component]:
    """A row per setting, under its section's heading; the advanced ones folded away."""
    by_section: dict[models.SettingsSection, list[Component]] = {}
    for name, field in fields.items():
        by_section.setdefault(field.section, []).append(
            _row(name, field, current, catalog, _applies(name, unlocked))
        )
    head = html.Div(
        [html.Span("Set"), html.Span("Setting"), html.Span("Value")],
        className="dl-chart-settings-row dl-chart-settings-head",
    )
    shown: list[Component] = [head]
    for section, rows in by_section.items():
        if section is not models.ADVANCED_SECTION:
            shown += [html.Div(section.title, className="dl-chart-settings-section"), *rows]
    if advanced := by_section.get(models.ADVANCED_SECTION):
        summary = html.Summary(
            f"{models.ADVANCED_SECTION.title} ({len(advanced)})", className="dl-chart-settings-section"
        )
        shown.append(html.Details([summary, *advanced], className="dl-chart-settings-advanced"))
    return shown


def _purpose(target: SettingsTarget) -> tuple[str, str]:
    """Which charts the form is for, and what Apply does to them."""
    once = "A ticked setting is written into each of these charts, once: charts added later keep their own."
    match target.panel, bool(target.query):
        case None, False:
            return "Every chart on this page", (
                "A ticked setting replaces each chart's own, for charts added later too. A panel's chart settings "
                "take precedence over the page's. Leave a setting unticked to leave it to each chart."
            )
        case str() as panel, False:
            return f"Every chart in {panel}", (
                "A ticked setting replaces each chart's own, and the page's chart settings, for charts added to this "
                "panel later too. Leave a setting unticked to leave it to each chart."
            )
        case None, True:
            return f"Charts in panels matching “{target.query}”", (
                f"{once} Clear the panel filter to set it for the whole page instead."
            )
        case str() as panel, True:
            return f"Charts in {panel} matching “{target.query}”", (
                f"{once} Clear the chart filter to set it for the whole panel instead."
            )
    msg = "unreachable"
    raise AssertionError(msg)


def _scope(target: SettingsTarget, reached: list[models.ChartInstance[Any, Any]]) -> dmc.Paper:
    """What the form is for, above its fields: which charts, how many of each type, and what Apply does to them."""
    unlocked = [chart for chart in reached if not chart.locked]
    locked = len(reached) - len(unlocked)
    where, how = _purpose(target)
    kinds = Counter(_kind(chart.chart_type) for chart in unlocked)
    badges = [
        dmc.Badge(
            f"{count} {kind} chart{'' if count == 1 else 's'}", variant="light", color="gray", tt="none"
        )
        for kind, count in sorted(kinds.items())
    ]
    if locked:
        badges.append(
            dmc.Badge(
                f"{locked} locked: keeping their own",
                leftSection=icon(Icon.KEPT),
                variant="light",
                color="yellow",
                tt="none",
            )
        )
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Text(where, size="sm", fw=600),
                        dmc.Text(f"· {_charts(len(unlocked))}", size="sm", c="dimmed"),
                    ],
                    gap=6,
                ),
                dmc.Group(badges, gap=6),
                dmc.Text(how, size="xs", c="dimmed"),
            ],
            gap=6,
        ),
        withBorder=True,
        radius="md",
        p="sm",
    )


def _opened(page_json: str, target: SettingsTarget) -> tuple[Any, ...]:
    """The modal opened on `target`: its target, what it is for, and its form; or a notice why there is nothing to set."""
    page = page_model.BasicExperimentPage.model_validate_json(page_json)
    reached = _reached(page, target)
    fields = layerable_fields(chart.chart_type for chart in reached)
    if not fields:
        why = (
            "No chart matches the filter."
            if target.query and not reached
            else "No chart here has a setting to set."
        )
        return False, no_update, no_update, no_update, notice("No settings to set", why)
    catalog = ColumnCatalog.of_page(get_data_store(), page)
    unlocked = [chart for chart in reached if not chart.locked]
    form = _form(fields, _current(page, target), catalog, unlocked)
    return True, target.model_dump(), _scope(target, reached), form, no_update


def _applied(ctx: _ApplyCtx, target: SettingsTarget) -> edits.PageEdit:
    """The edit the form makes: the panel's or the page's chart settings, or what is ticked written into the charts."""
    stored = load_page(
        get_data_store(), ref_of(page_model.BasicExperimentPage.model_validate_json(ctx.page_json))
    )
    fields = layerable_fields(chart.chart_type for chart in _reached(stored, target))
    values = merge_chart_param_values(ctx.values, ctx.checked_values, ctx.field_ids, fields)
    shown = [fid["field"] for fid in ctx.set_ids]
    chosen = {
        name: values[name]
        for name, ticked in zip(shown, ctx.set_checked, strict=True)
        if ticked and values.get(name) is not None
    }
    if target.query:
        query = target.query if target.panel is not None else ""

        def written(panel: models.PanelInstance[Any, Any]) -> models.PanelInstance[Any, Any]:
            charts = [
                chart.model_copy(update={"parameters": chart.drawn_with((chosen,)).parameters})
                if chart_matches(chart, query)
                else chart
                for chart in panel.charts
            ]
            return panel.model_copy(update={"charts": charts})

        return edits.PageEdit(
            panels=lambda panels: [written(p) if _reaches(target, p) else p for p in panels]
        )
    if target.panel is None:
        settings = layered(stored.document.chart_settings, shown, chosen)
        return edits.PageEdit(document=page_model.DocumentSettings(chart_settings=settings))
    return edits.PageEdit(
        panels=lambda panels: [
            p.model_copy(update={"chart_settings": layered(p.chart_settings, shown, chosen)})
            if p.name == target.panel
            else p
            for p in panels
        ]
    )


def _lock_toggled(panel: str, index: int) -> edits.PanelsEdit:
    """An edit of the panels: the chart at `index` of `panel` locked if it was not, or unlocked."""

    def toggled(panels: list[models.PanelInstance[Any, Any]]) -> list[models.PanelInstance[Any, Any]]:
        return [
            p
            if p.name != panel
            else p.model_copy(
                update={
                    "charts": [
                        c.model_copy(update={"locked": not c.locked}) if i == index else c
                        for i, c in enumerate(p.charts)
                    ]
                }
            )
            for p in panels
        ]

    return toggled


def register_chart_settings(app: Dash) -> None:
    """The form's open, apply and cancel, and the lock on a chart's card."""
    _register_open(app)
    _register_apply(app)
    _register_lock(app)
    app.clientside_callback(
        _SET_ON_CHANGE_JS.source,
        Output(LAYER_SET.match(), "checked"),
        Input(LAYER_PARAM.match(), "value"),
        Input(LAYER_PARAM.match(), "checked"),
        prevent_initial_call=True,
    )


def _register_open(app: Dash) -> None:
    opened_outputs = [
        Output(SETTINGS_MODAL_ID, "opened", allow_duplicate=True),
        Output(SETTINGS_TARGET_ID, "data", allow_duplicate=True),
        Output(SETTINGS_BODY_ID, "children", allow_duplicate=True),
        Output(SETTINGS_FORM_ID, "children", allow_duplicate=True),
        Output(VIEW_NOTIFY_ID, "sendNotifications", allow_duplicate=True),
    ]

    @app.callback(
        *opened_outputs,
        Input(panel_view.PANEL_CHART_SETTINGS_BUTTON.all(), "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def open_panel_settings(_clicks: list[int | None], page_json: str, paging_json: str) -> tuple[Any, ...]:
        panel = require_triggered_id(panel_view.PanelID).panel
        paging = Paging.model_validate_json(paging_json)
        tab = paging.shown_tab(page_model.BasicExperimentPage.model_validate_json(page_json).panels)
        return _opened(
            page_json, SettingsTarget(panel=panel, query=paging.chart_paging(panel).q.strip(), tab=tab)
        )

    @app.callback(
        *opened_outputs,
        Input(panel_view.PAGE_CHART_SETTINGS_BUTTON_ID, "n_clicks"),
        State(ids.STATE_PAGE_STORAGE, "data"),
        State(ids.PANEL_PAGING_ID, "data"),
        prevent_initial_call=True,
    )
    def open_page_settings(n_clicks: int | None, page_json: str, paging_json: str) -> tuple[Any, ...]:
        if not n_clicks:
            raise PreventUpdate
        paging = Paging.model_validate_json(paging_json)
        tab = paging.shown_tab(page_model.BasicExperimentPage.model_validate_json(page_json).panels)
        return _opened(page_json, SettingsTarget(panel=None, query=paging.panel_q.strip(), tab=tab))


def _register_apply(app: Dash) -> None:
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Output(SETTINGS_MODAL_ID, "opened", allow_duplicate=True),
        Input(SETTINGS_APPLY_ID, "n_clicks"),
        _ApplyCtx.states(),
        prevent_initial_call=True,
        running=busy_while_running(SETTINGS_APPLY_ID),
    )
    def apply_settings(n_clicks: int | None, states: dict[str, Any]) -> tuple[html.Div, str, bool]:
        ctx = _ApplyCtx.model_validate(states)
        if not n_clicks or ctx.target is None:
            raise PreventUpdate
        saved, container = edits.commit(ctx.page_json, ctx.paging_json, _applied(ctx, ctx.target))
        return container, saved.model_dump_json(), False

    @app.callback(
        Output(SETTINGS_MODAL_ID, "opened", allow_duplicate=True),
        Input(SETTINGS_CANCEL_ID, "n_clicks"),
        prevent_initial_call=True,
    )
    def cancel_settings(n_clicks: int | None) -> bool:
        if not n_clicks:
            raise PreventUpdate
        return False


def _register_lock(app: Dash) -> None:
    @app.callback(
        Output(ids.METRIC_CONTENT_ID, "children", allow_duplicate=True),
        Output(ids.STATE_PAGE_STORAGE, "data", allow_duplicate=True),
        Input(chart_view.LOCK_CHART_BUTTON.all(), "n_clicks"),
        edits.EditCtx.states(),
        prevent_initial_call=True,
    )
    def toggle_lock(clicks: list[int | None], states: dict[str, Any]) -> tuple[html.Div | NoUpdate, str]:
        """Lock the chart, so it is drawn with its own settings whatever its panel's and page's say; or unlock it."""
        ctx = edits.EditCtx.model_validate(states)
        at = require_triggered_id(chart_view.ChartID)
        if not any(clicks) or at.index is None:
            raise PreventUpdate
        saved, container = edits.commit(
            ctx.page_json,
            ctx.paging_json,
            edits.PageEdit(
                panels=_lock_toggled(at.panel, at.index), reveal=edits.RevealChartAt(at.panel, at.index)
            ),
        )
        return container, saved.model_dump_json()
