"""
A run's console output (`dlboard.client._console`), a chunk at a time: each run's newest chunk first, then older ones.

The panel reads each run's newest chunk alone (`WindowAt.LATEST`), with which steps it logged; the browser fetches the
chunk on show by its URL and draws it (`console.js`): a JSON line (structlog's, say) with its level and message pulled
out, anything else as a terminal would show it -- its colours (ANSI escapes) drawn, and a line redrawn in place (a
progress bar's carriage returns) as it was last drawn. Only the lines in view are in the page, so a long chunk scrolls
as fast as a short one. Older and Newer read the chunk beside the one on show, of one run: a read of a bounded span of
steps around it. Nothing reads or searches more than one chunk: the filter narrows the lines of the one on show.
"""

from __future__ import annotations

import hashlib
import json
import typing
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import dcc, html
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field, JsonValue

from dlboard.models import (
    CONSOLE_KEY,
    DISPLAY_SECTION,
    EXPERIMENT_COLUMN,
    RUN_NAME_COLUMN,
    ArtifactWindow,
    ChartType,
    ColumnKind,
    DataRequest,
    MetricColumn,
    RenderContext,
    StepRange,
    WindowAt,
    artifact_column,
    artifact_logged_column,
    artifact_tags_column,
)
from dlboard.plugins.charts._artifact_tags import parse_tags
from dlboard.serve import AssetKind, ClientsideScript, build_artifacts_dataframe, get_data_store, serve_asset
from dlboard.serve._pages._dash_helpers import require_triggered_id

if typing.TYPE_CHECKING:
    from dlboard.models import Artifact, DataStore

_JS: Final = ClientsideScript(Path(__file__).with_name("console.js"))
_CSS_PATH: Final = Path(__file__).with_name("console.css")

_RUN: Final = "dl-console-run"
_SHOWN: Final = "dl-console-shown"
_OPENING: Final = "dl-console-opening"
_PAGE: Final = "dl-console-page"
_FILTER: Final = "dl-console-filter"
_LINES: Final = "dl-console-lines"
SEEK_STEPS: Final = 64
"""How far Older/Newer look for the chunk beside the one on show: a chunk lost on its way to the server is skipped."""


class ConsoleOrder(StrEnum):
    """Which way a run's output reads: which chunk the chart opens on, and which of its lines is at the top."""

    NEWEST_FIRST = "newest_first"
    """The newest chunk, its last line at the top: scrolling down, then Older, goes back in time."""
    OLDEST_FIRST = "oldest_first"
    """The run's first chunk, its lines as printed: scrolling down, then Newer, goes forward."""


class ConsoleChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Console settings."""

    key: str = CONSOLE_KEY
    """The artifact key the console is logged under."""
    order: ConsoleOrder = ConsoleOrder.NEWEST_FIRST
    """Newest first (the newest chunk, its latest line at the top) or oldest first (the first chunk, as printed)."""
    height: typing.Annotated[int, DISPLAY_SECTION] = Field(default=360, ge=120)
    """Height of the text, in px; a longer chunk scrolls."""
    wrap: typing.Annotated[bool, DISPLAY_SECTION] = True
    """Wrap a line longer than the chart is wide onto the next; off, a long line scrolls sideways."""
    width: typing.Annotated[int, DISPLAY_SECTION] = Field(default=760, ge=320)
    """Width in px where the panel packs its charts; in a grid, the chart fills its cell."""


class _PageWay(StrEnum):
    """Where a pager button goes from the chunk on show: a chunk back or on, or to the run's first or newest."""

    OLDEST = "oldest"
    OLDER = "older"
    NEWER = "newer"
    NEWEST = "newest"


class Shown(BaseModel, frozen=True, extra="forbid"):
    """The chunk on show, of which run: what the browser fetches and draws, and what Older and Newer read beside."""

    experiment_id: int
    run_id: int
    key: str
    step: int
    url: str
    tags: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """What the client said of the chunk: output dropped before it (`dropped_chars`), the run's cap reached (`capped`)."""
    first_step: int
    """The run's first chunk, as last read: where the chunk on show is among them (`Chunk 3 of 40`)."""
    last_step: int
    """The run's newest chunk, as last read."""
    way: _PageWay | None = None
    """
    The pager button that went to it, if one did: `console.js` opens it where reading goes on from the chunk before.

    That is its end when the chunk read before it is at its end: Newer, newest first (the chunk's first line is at the
    bottom), and Older, oldest first. Anything else opens it at the top.
    """


_PAGER: Final = (
    (_PageWay.OLDEST, "⇤ Oldest", "The run's first chunk"),
    (_PageWay.OLDER, "← Older", "The chunk before this one"),
    (_PageWay.NEWER, "Newer →", "The chunk after this one"),
    (_PageWay.NEWEST, "Newest ⇥", "The run's newest chunk"),
)
"""The pager's buttons, oldest to newest left to right whichever way the chart reads, with what each says and does."""


class _PageId(BaseModel, frozen=True, extra="forbid"):
    """A pager button's id, as the browser sends it back when it is clicked."""

    type: str
    instance: str
    way: _PageWay


class _Opening(BaseModel, frozen=True, extra="forbid"):
    """The chunk each run opens on, by run id: what picking a run shows, with nothing read."""

    runs: dict[str, Shown]


def _instance_id(parameters: ConsoleChartSettings, chart_id: str) -> str:
    """One per chart and settings, as a Plotly chart's: the editor's preview of a chart must not share its ids."""
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def _opening_window(parameters: ConsoleChartSettings) -> ArtifactWindow:
    """The one chunk of each run the panel reads the URL of: its newest, or its first (`ConsoleOrder`)."""
    match parameters.order:
        case ConsoleOrder.NEWEST_FIRST:
            at = WindowAt.LATEST
        case ConsoleOrder.OLDEST_FIRST:
            at = WindowAt.EARLIEST
    return ArtifactWindow(parameters.key, 1, at=at)


def opening_chunks(parameters: ConsoleChartSettings, dataframe: pd.DataFrame) -> dict[int, Shown]:
    """
    The chunk each run opens on, the one the panel read the URL of, by run id, in the order the frame lists the runs.

    The frame lists every chunk each run logged (`artifact_logged_column`), which says where the run's first and newest are.
    """
    url_column = artifact_column(parameters.key)
    if dataframe.empty or url_column not in dataframe.columns:
        return {}
    tags_column = artifact_tags_column(parameters.key)
    logged_column = artifact_logged_column(parameters.key)
    logged = dataframe[dataframe[logged_column].notna()] if logged_column in dataframe else dataframe
    steps: dict[int, list[int]] = {}
    for run, step in logged[[MetricColumn.RUN_ID, MetricColumn.STEP]].itertuples(index=False):
        steps.setdefault(int(run), []).append(int(step))
    opening = dataframe[dataframe[url_column].notna()].drop_duplicates(MetricColumn.RUN_ID)
    order = list(dict.fromkeys(int(run) for run in dataframe[MetricColumn.RUN_ID]))
    found = {
        (run := int(row[MetricColumn.RUN_ID])): Shown(
            experiment_id=int(row[EXPERIMENT_COLUMN]),
            run_id=run,
            key=parameters.key,
            step=(step := int(row[MetricColumn.STEP])),
            url=str(row[url_column]),
            tags=dict(parse_tags(row[tags_column])) if tags_column in opening.columns else {},
            first_step=min(steps.get(run, [step])),
            last_step=max(steps.get(run, [step])),
        )
        for _, row in opening.iterrows()
    }
    return {run: found[run] for run in order if run in found}


def _run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    named = dataframe.drop_duplicates(MetricColumn.RUN_ID)[[MetricColumn.RUN_ID, RUN_NAME_COLUMN]].dropna()
    return {int(run): str(name) for run, name in named.itertuples(index=False)}


def paged(store: DataStore[...], shown: Shown, way: _PageWay) -> Shown | None:
    """
    The chunk `way` goes to from the one on show, of its run, or `None` if there is none (nearby, for Older and Newer).

    Older and Newer read a bounded span of steps beside it. Oldest and Newest list the run's steps (two numbers a chunk,
    not a chunk) to find its ends now, which a run still printing has moved on from since the panel read them.
    """
    run = frozenset({shown.run_id})
    match way:
        case _PageWay.OLDER:
            span = StepRange(max(0, shown.step - SEEK_STEPS), shown.step - 1)
        case _PageWay.NEWER:
            span = StepRange(shown.step + 1, shown.step + SEEK_STEPS)
        case _PageWay.OLDEST | _PageWay.NEWEST:
            logged = store.artifact_steps(shown.experiment_id, key=shown.key, run_ids=run).get(
                shown.run_id, []
            )
            if not logged:
                return None
            shown = shown.model_copy(update={"first_step": logged[0], "last_step": logged[-1]})
            end = logged[0] if way is _PageWay.OLDEST else logged[-1]
            span = StepRange(end, end)
    if span.last < span.first:
        return None
    found = {
        artifact.step: artifact
        for artifact in store.fetch_artifacts(
            shown.experiment_id, keys=frozenset({shown.key}), run_ids=run, steps=span
        )
        if artifact.step is not None
    }
    if not found:
        return None
    step = max(found) if way is _PageWay.OLDER else min(found)
    url = str(build_artifacts_dataframe([found[step]])[artifact_column(shown.key)].iloc[0])
    return shown.model_copy(
        update={
            "step": step,
            "url": url,
            "tags": dict(found[step].tags),
            "first_step": min(shown.first_step, step),
            "last_step": max(shown.last_step, step),
            "way": way,
        }
    )


class ConsoleChart(ChartType[ConsoleChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """A run's console output, a chunk at a time, with a filter over the lines of the one on show."""

    name: typing.ClassVar[str] = "console"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[ConsoleChartSettings]:
        return ConsoleChartSettings

    @typing.override
    @classmethod
    def can_display_artifact(cls, artifact: Artifact) -> bool:
        """A run's console output: what the client logs under `CONSOLE_KEY`."""
        return artifact.key == CONSOLE_KEY

    @typing.override
    @classmethod
    def render(cls, parameters: ConsoleChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, RenderContext())

    @typing.override
    @classmethod
    def render_in(
        cls, parameters: ConsoleChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        opening = opening_chunks(parameters, dataframe)
        if not opening:
            return dmc.Stack([dmc.Text("Nothing was printed in the runs shown", c="dimmed")])
        instance = _instance_id(parameters, context.chart_id)
        names = _run_names(dataframe)
        first = next(iter(opening.values()))
        at = cast("dict[str, Any]", {"data-dl-instance": instance})

        return dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Select(
                            id={"type": _RUN, "instance": instance},
                            data=[
                                {"value": str(run), "label": names.get(run, f"Run {run}")} for run in opening
                            ],
                            value=str(first.run_id),
                            allowDeselect=False,
                            size="xs",
                            w=200,
                        ),
                        dmc.TextInput(
                            id={"type": _FILTER, "instance": instance},
                            placeholder="Filter these lines",
                            size="xs",
                            debounce=200,
                            style={"flex": 1},
                        ),
                    ],
                    gap="xs",
                    wrap="nowrap",
                ),
                # A pager over the run's chunks, oldest to newest left to right; where the one on show is, beside it.
                dmc.Group(
                    [
                        dmc.Group(
                            [
                                dmc.Tooltip(
                                    dmc.Button(
                                        label,
                                        id={"type": _PAGE, "instance": instance, "way": way},
                                        size="compact-xs",
                                        variant="default",
                                    ),
                                    label=does,
                                    openDelay=400,
                                )
                                for way, label, does in _PAGER
                            ],
                            gap=4,
                        ),
                        html.Span(className="dl-console-position", **at),
                    ],
                    gap="sm",
                ),
                # Drawn by `console.js` itself, a screenful of lines at a time: Dash never sets its children.
                html.Div(
                    id={"type": _LINES, "instance": instance},
                    className="dl-console",
                    style={"height": parameters.height},
                    **at,
                    **cast(
                        "dict[str, Any]",
                        {
                            "data-dl-order": parameters.order.value,
                            "data-dl-wrap": str(parameters.wrap).lower(),
                        },
                    ),
                ),
                dcc.Store(id={"type": _SHOWN, "instance": instance}, data=first.model_dump()),
                dcc.Store(
                    id={"type": _OPENING, "instance": instance},
                    data=_Opening(runs={str(run): shown for run, shown in opening.items()}).model_dump(),
                ),
            ],
            gap="xs",
        )

    @typing.override
    @classmethod
    def data_request(cls, parameters: ConsoleChartSettings) -> DataRequest:
        # Every step a run logged is listed, but only the chunk it opens on read: the rest are read one at a time.
        return DataRequest(
            artifacts=frozenset({parameters.key}), artifact_windows=frozenset({_opening_window(parameters)})
        )

    @typing.override
    @classmethod
    def natural_width(cls, parameters: ConsoleChartSettings) -> int:
        return parameters.width

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT}


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type, the callbacks that pick and page its chunks, and the script that draws one."""
    ConsoleChart.register(allow_override=True)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())

    @app.callback(
        dash.Output({"type": _SHOWN, "instance": dash.MATCH}, "data", allow_duplicate=True),
        dash.Input({"type": _RUN, "instance": dash.MATCH}, "value"),
        dash.State({"type": _OPENING, "instance": dash.MATCH}, "data"),
        prevent_initial_call=True,
    )
    def show_run(run: str | None, opening: dict[str, Any]) -> dict[str, Any]:
        """A run picked: the chunk it opens on, which the chart already has."""
        runs = _Opening.model_validate(opening).runs
        if run is None or run not in runs:
            raise PreventUpdate
        return runs[run].model_dump()

    @app.callback(
        dash.Output({"type": _SHOWN, "instance": dash.MATCH}, "data", allow_duplicate=True),
        dash.Input({"type": _PAGE, "instance": dash.MATCH, "way": dash.ALL}, "n_clicks"),
        dash.State({"type": _SHOWN, "instance": dash.MATCH}, "data"),
        prevent_initial_call=True,
    )
    def page_console(clicks: list[int | None], shown: dict[str, Any]) -> dict[str, Any]:
        """A pager button: the chunk it goes to from the one on show, of its run."""
        on_show = Shown.model_validate(shown)
        del clicks
        found = paged(get_data_store(), on_show, require_triggered_id(_PageId).way)
        if found is None:
            raise PreventUpdate
        return found.model_dump()

    # Side effect only: it draws into the element itself, and the prop it returns says what it drew.
    app.clientside_callback(
        _JS.source,
        dash.Output({"type": _LINES, "instance": dash.MATCH}, "data-dl-drawn"),
        dash.Input({"type": _SHOWN, "instance": dash.MATCH}, "data"),
        dash.Input({"type": _FILTER, "instance": dash.MATCH}, "value"),
    )
