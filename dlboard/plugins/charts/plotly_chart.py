"""
A Plotly figure logged as an artifact (`client.artifacts.plotly.PlotlyFigure`), drawn in the browser as the figure it is.

The server never reads a figure: it hands the browser each run's figure URL at each step, as an image chart does its
pictures, and `plotly_chart.js` fetches the one for the step on show, dresses it in the page's theme and draws it.
"""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import Annotated, Any, Final, cast

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import dcc
from pydantic import BaseModel, Field

from dlboard._compat import StrEnum
from dlboard.models import (
    ADVANCED_SECTION,
    DEFAULT_RENDER_CONTEXT,
    DISPLAY_SECTION,
    PLOTLY_FIGURE_EXTENSION,
    RUN_NAME_COLUMN,
    Artifact,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    RenderContext,
    artifact_column,
    artifact_display_name,
    plotly_data_key,
)
from dlboard.plugins.charts._empty_chart import nothing_logged
from dlboard.plugins.charts.image_series import slider_marks
from dlboard.serve import ClientsideScript, series_color, series_swatch_class
from dlboard.serve._series import SERIES_SLOTS

COMBINED_RUNS: Final = "combined"
"""The `run` of the one graph a chart with `combine_runs` draws every run in: never a run's id, which is a number."""

_TEXT: Final = "var(--mantine-color-text)"
_LINES: Final = "var(--mantine-color-default-border)"
_CLEAR: Final = "rgba(0,0,0,0)"
_AXIS: Final = {"gridcolor": _LINES, "linecolor": _LINES, "zerolinecolor": _LINES, "automargin": True}

DLBOARD_THEME: Final = {
    "layout": {
        "paper_bgcolor": _CLEAR,
        "plot_bgcolor": _CLEAR,
        "font": {"color": _TEXT},
        "colorway": [series_color(slot) for slot in range(SERIES_SLOTS)],
        "xaxis": _AXIS,
        "yaxis": _AXIS,
        "legend": {"bgcolor": _CLEAR},
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
    }
}
"""
The Plotly template a figure is drawn with in place of its own: the page's colors, in either color scheme.

A template only supplies defaults, so whatever the figure sets itself (a trace's color, an axis title) is kept. Its
colors are CSS variables, which Plotly cannot read: the browser resolves them (`plotly_chart.js`).
"""


class StreamMode(StrEnum):
    """Which steps of the data streamed into a template a chart draws."""

    ACCUMULATE = "accumulate"
    """Every step's, in step order: a column of values grows by each step's, a 2-D one by its rows (a trajectory)."""
    LATEST = "latest"
    """Each run's latest step's alone (a confusion matrix as it stands)."""
    SCRUB = "scrub"
    """One step's at a time, with a slider over the steps."""


DEFAULT_MAX_DATA_STEPS: Final = 256


class PlotlySettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Plotly figure settings."""

    key: str
    """The key the figure, or the template, is logged under."""
    data_steps: StreamMode = StreamMode.ACCUMULATE
    """For a template, which steps of the data logged into it are drawn: every one, the latest, or one at a time."""
    combine_runs: Annotated[bool, DISPLAY_SECTION] = False
    """Draw every run's traces in one figure, a legend group each, rather than a figure per run."""
    height: Annotated[int, DISPLAY_SECTION] = Field(default=360, ge=100)
    """Height of each figure, in px."""
    width: Annotated[int | None, DISPLAY_SECTION] = None
    """Overall chart width in px. Leave blank to size it from the height and the panel's columns."""
    use_figure_theme: Annotated[bool, DISPLAY_SECTION] = False
    """Draw the figure in the theme it was logged with, rather than in the page's colors."""
    keep_view_across_steps: Annotated[bool, DISPLAY_SECTION] = True
    """Keep where you zoomed and panned, and the traces you hid, as the slider moves to another step."""
    max_data_steps: Annotated[int, ADVANCED_SECTION] = Field(default=DEFAULT_MAX_DATA_STEPS, ge=1)
    """The most steps of a template's data drawn per run when every step's is: a longer run is thinned to every 2nd, 4th, ... step, and its latest."""


class _RunFigures(BaseModel, frozen=True, extra="forbid"):
    name: str
    swatch: str
    color: str
    urls: dict[int, str]
    """By step: the run's figure, or for a template the data it logged."""


class _Figures(BaseModel, frozen=True, extra="forbid"):
    """What the browser draws a chart from (`plotly_chart.js`)."""

    runs: dict[int, _RunFigures]
    order: list[int]
    """The runs, in the order the combined figure lists them."""
    steps: list[int]
    """The steps the slider goes to; one, and no slider, for a template drawn whole."""
    theme: dict[str, Any] | None
    """The Plotly template the figures are drawn with, or `None` for their own."""
    keep_view: bool
    template: str | None = None
    """The URL of the figure a template's data is drawn into; `None` for figures drawn as they were logged."""
    data_steps: StreamMode | None = None
    thinned: int = 0
    """How many runs had their data thinned to `max_data_steps`."""


def thinned(steps: list[int], most: int) -> list[int]:
    """
    About `most` of `steps` (sorted): every `stride`-th from the first, and always the last.

    `stride` is the least power of two that fits, so the steps kept stay put as a run logs more, until it doubles,
    rather than shifting at every refresh (as `fetch_histograms` thins).
    """
    stride = 1
    while -(-len(steps) // stride) > most:
        stride *= 2
    kept = steps[::stride]
    return kept if kept[-1] == steps[-1] else [*kept, steps[-1]]


_GRAPH: Final = "plotly-chart-graph"
_DATA: Final = "plotly-chart-data"
_STEP: Final = "plotly-chart-step"
_SLIDER: Final = "plotly-chart-slider"
_PAGE: Final = "plotly-chart-page"
_PAGER: Final = "plotly-chart-pager"

_GRAPH_CONFIG: Final = {"displaylogo": False, "responsive": True}
_EMPTY_FIGURE: Final[dict[str, Any]] = {
    "data": [],
    "layout": {"xaxis": {"visible": False}, "yaxis": {"visible": False}},
}
"""What a graph shows until its figure has been fetched: nothing, rather than a pair of empty axes."""


def _instance_id(parameters: PlotlySettings, chart_id: str) -> str:
    """One per chart and settings, as an image chart's: the editor's preview of a chart must not share its ids."""
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def _run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    """What to call each run of the frame that has a name; a run with none is called by its id."""
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    rows = dataframe.drop_duplicates(MetricColumn.RUN_ID)[[MetricColumn.RUN_ID, RUN_NAME_COLUMN]]
    return {int(run_id): name for run_id, name in rows.itertuples(index=False) if isinstance(name, str)}


def _logged(dataframe: pd.DataFrame, key: str) -> dict[int, dict[int, str]]:
    """The URL of each run's artifact under `key` at each step it logged one, by run and step, in step order."""
    col = artifact_column(key)
    if col not in dataframe.columns:
        return {}
    urls: dict[int, dict[int, str]] = {}
    rows = dataframe.loc[dataframe[col].notna(), [MetricColumn.RUN_ID, MetricColumn.STEP, col]]
    for run_id, step, url in rows.sort_values(MetricColumn.STEP).itertuples(index=False):
        urls.setdefault(int(run_id), {})[int(step)] = str(url)
    return urls


def _streamed(
    data: dict[int, dict[int, str]], parameters: PlotlySettings
) -> tuple[dict[int, dict[int, str]], int]:
    """Each run's data at the steps `parameters.data_steps` draws, and how many runs were thinned to get there."""
    match parameters.data_steps:
        case StreamMode.SCRUB:
            return data, 0
        case StreamMode.LATEST:
            return {run: dict([max(urls.items())]) for run, urls in data.items()}, 0
        case StreamMode.ACCUMULATE:
            kept = {run: thinned(list(urls), parameters.max_data_steps) for run, urls in data.items()}
            return (
                {run: {step: data[run][step] for step in steps} for run, steps in kept.items()},
                sum(len(steps) < len(data[run]) for run, steps in kept.items()),
            )


def _figures(parameters: PlotlySettings, dataframe: pd.DataFrame) -> _Figures | None:
    """
    What the browser draws, from the frame: each run's figures, or for a template each run's data; `None` for neither.

    A key is a template once data has been logged into it (`plotly_data_key`): its figure is the newest run's latest,
    and each run is drawn from the data it logged.
    """
    figures = _logged(dataframe, parameters.key)
    data = _logged(dataframe, plotly_data_key(parameters.key))
    if not figures:
        return None
    newest = figures[max(figures)]
    template = newest[max(newest)] if data else None
    by_run, thinned_runs = _streamed(data, parameters) if data else (figures, 0)
    names = _run_names(dataframe)
    steps = sorted({step for urls in by_run.values() for step in urls})
    return _Figures(
        runs={
            run_id: _RunFigures(
                name=names.get(run_id, f"Run {run_id}"),
                swatch=series_swatch_class(run_id),
                color=series_color(run_id),
                urls=urls,
            )
            for run_id, urls in by_run.items()
        },
        order=sorted(by_run),
        steps=steps if template is None or parameters.data_steps is StreamMode.SCRUB else steps[-1:],
        theme=None if parameters.use_figure_theme else DLBOARD_THEME,
        keep_view=parameters.keep_view_across_steps,
        template=template,
        data_steps=parameters.data_steps if template is not None else None,
        thinned=thinned_runs,
    )


def _thinned_note(runs: int, most: int) -> dmc.Text:
    """What a template chart says under itself when it drew only some of the steps a run logged data at."""
    return dmc.Text(
        f"{runs:,} run{'s' if runs > 1 else ''} logged data at more than {most:,} steps, so every few of them are "
        "drawn, and the latest: raise the chart's max data steps, or log the data less often",
        size="xs",
        c="dimmed",
    )


def _graph(instance: str, run: str, height: int) -> dcc.Graph:
    return dcc.Graph(
        id={"type": _GRAPH, "instance": instance, "run": run},
        figure=_EMPTY_FIGURE,
        config=_GRAPH_CONFIG,
        style={"height": f"{height}px", "width": "100%"},
    )


def _run_grids(
    figures: _Figures, parameters: PlotlySettings, instance: str, context: RenderContext
) -> list[Any]:
    """A figure per run, a page of `context.page_size` runs at a time, laid out in the panel's columns."""
    pages = [
        figures.order[i : i + context.page_size] for i in range(0, len(figures.order), context.page_size)
    ]

    def run_block(run_id: int) -> dmc.Stack:
        run = figures.runs[run_id]
        return dmc.Stack(
            [
                dmc.Text(run.name, size="sm", fw=600, className=run.swatch),
                _graph(instance, str(run_id), parameters.height),
            ],
            gap=4,
        )

    grids: list[Any] = [
        dmc.SimpleGrid(
            id={"type": _PAGE, "instance": instance, "page": index},
            # No more columns than runs, so a lone run's figure gets the chart's whole width.
            cols=min(context.columns, len(runs)),
            **cast("dict[str, Any]", {"data-dl-follow-panel-columns": "1"}),
            style={} if index == 0 else {"display": "none"},
            children=[run_block(run_id) for run_id in runs],
        )
        for index, runs in enumerate(pages)
    ]
    if len(pages) > 1:
        grids.append(
            dmc.Pagination(id={"type": _PAGER, "instance": instance}, total=len(pages), value=1, mt="sm")
        )
    return grids


class PlotlyChart(ChartType[PlotlySettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """A logged Plotly figure, interactive: one per run, or every run's in one, with a slider over the steps logged."""

    name: typing.ClassVar[str] = "plotly"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[PlotlySettings]:
        return PlotlySettings

    @typing.override
    @classmethod
    def can_display_artifact(cls, artifact: Artifact) -> bool:
        """Figures logged as `PlotlyFigure`s, by their extension."""
        return artifact.fname.endswith(PLOTLY_FIGURE_EXTENSION)

    @typing.override
    @classmethod
    def render(cls, parameters: PlotlySettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @typing.override
    @classmethod
    def render_in(
        cls, parameters: PlotlySettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        figures = _figures(parameters, dataframe)
        if figures is None:
            return nothing_logged(parameters.key)
        instance = _instance_id(parameters, context.chart_id)
        first = figures.steps[0]
        return dmc.Stack(
            [
                dmc.Text(artifact_display_name(parameters.key), fw=700, style={"overflowWrap": "anywhere"}),
                dcc.Store(id={"type": _DATA, "instance": instance}, data=figures.model_dump(mode="json")),
                # The step on show: the slider's, or the one step logged when there is no slider.
                dcc.Store(id={"type": _STEP, "instance": instance}, data=first),
                *(
                    [
                        dmc.Slider(
                            id={"type": _SLIDER, "instance": instance},
                            min=first,
                            max=figures.steps[-1],
                            value=first,
                            marks=slider_marks(figures.steps),  # pyrefly: ignore [bad-argument-type]
                            restrictToMarks=True,
                            persistence=True,
                            persistence_type="session",
                            persisted_props=["value"],
                            mb="xl",
                        )
                    ]
                    if len(figures.steps) > 1
                    else []
                ),
                *(
                    [_graph(instance, COMBINED_RUNS, parameters.height)]
                    if parameters.combine_runs
                    else _run_grids(figures, parameters, instance, context)
                ),
                *([_thinned_note(figures.thinned, parameters.max_data_steps)] if figures.thinned else []),
            ],
            gap="sm",
        )

    @typing.override
    @classmethod
    def data_request(cls, parameters: PlotlySettings) -> DataRequest:
        # The data logged into the key, if it is a template, is under a key of its own.
        return DataRequest(
            artifacts=frozenset({parameters.key}),
            artifact_prefixes=frozenset({plotly_data_key(parameters.key)}),
        )

    @typing.override
    @classmethod
    def natural_width(cls, parameters: PlotlySettings) -> int:
        return cls.natural_width_in(parameters, DEFAULT_RENDER_CONTEXT)

    @typing.override
    @classmethod
    def natural_width_in(cls, parameters: PlotlySettings, context: RenderContext) -> int:
        figure_width = round(parameters.height * 4 / 3)
        return parameters.width or (
            figure_width if parameters.combine_runs else context.columns * figure_width
        )

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT}


_FIGURES_JS: Final = ClientsideScript(Path(__file__).with_name("plotly_chart.js"))
_STEP_JS: Final = ClientsideScript(Path(__file__).with_name("plotly_chart_step.js"))
# The image chart's: they move a synced panel's sliders together, and its paging, whatever the chart.
_LOCK_JS: Final = ClientsideScript(Path(__file__).with_name("image_series_lock.js"))
_PAGE_JS: Final = ClientsideScript(Path(__file__).with_name("image_series_page.js"))


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type, and the clientside callbacks that fetch and draw its figures."""
    PlotlyChart.register(allow_override=True)
    app.clientside_callback(
        _FIGURES_JS.source,
        dash.Output({"type": _GRAPH, "instance": dash.MATCH, "run": dash.ALL}, "figure"),
        dash.Input({"type": _STEP, "instance": dash.MATCH}, "data"),
        dash.Input({"type": _DATA, "instance": dash.MATCH}, "data"),
    )
    app.clientside_callback(
        _STEP_JS.source,
        dash.Output({"type": _STEP, "instance": dash.MATCH}, "data"),
        dash.Input({"type": _SLIDER, "instance": dash.MATCH}, "value"),
    )
    app.clientside_callback(
        _LOCK_JS.source,
        dash.Input({"type": _SLIDER, "instance": dash.ALL}, "value"),
        dash.State({"type": _SLIDER, "instance": dash.ALL}, "id"),
        dash.State({"type": _SLIDER, "instance": dash.ALL}, "marks"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _PAGE_JS.source,
        dash.Output({"type": _PAGE, "instance": dash.MATCH, "page": dash.ALL}, "style"),
        dash.Input({"type": _PAGER, "instance": dash.MATCH}, "value"),
        dash.State({"type": _PAGE, "instance": dash.MATCH, "page": dash.ALL}, "id"),
    )
