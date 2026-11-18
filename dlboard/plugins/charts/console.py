"""
A run's console output (`dlboard.client._console`), a chunk at a time: each run's newest chunk first, then older ones.

The panel reads each run's newest chunk alone (`WindowAt.LATEST`), with which steps it logged; the browser fetches the
chunk on show by its URL and draws it (`console.js`): a JSON line (structlog's, say) with its level and message pulled
out, anything else as written. Older and Newer read the chunk beside the one on show, of one run: a read of a bounded
span of steps around it. Nothing reads or searches more than one chunk: the filter narrows the lines of the one on show.
"""

from __future__ import annotations

import hashlib
import json
import typing
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

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
_LATEST: Final = "dl-console-latest"
_PAGE: Final = "dl-console-page"
_FILTER: Final = "dl-console-filter"
_LINES: Final = "dl-console-lines"
SEEK_STEPS: Final = 64
"""How far Older/Newer look for the chunk beside the one on show: a chunk lost on its way to the server is skipped."""


class ConsoleChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Console settings."""

    key: str = CONSOLE_KEY
    """The artifact key the console is logged under."""
    height: typing.Annotated[int, DISPLAY_SECTION] = Field(default=360, ge=120)
    """Height of the text, in px; a longer chunk scrolls."""
    width: typing.Annotated[int, DISPLAY_SECTION] = Field(default=760, ge=320)
    """Overall chart width in px."""


class Shown(BaseModel, frozen=True, extra="forbid"):
    """The chunk on show, of which run: what the browser fetches and draws, and what Older and Newer read beside."""

    experiment_id: int
    run_id: int
    key: str
    step: int
    url: str
    tags: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """What the client said of the chunk: output dropped before it (`dropped_chars`), the run's cap reached (`capped`)."""


class _PageWay(StrEnum):
    OLDER = "older"
    NEWER = "newer"


class _PageId(BaseModel, frozen=True, extra="forbid"):
    """An Older/Newer button's id, as the browser sends it back when it is clicked."""

    type: str
    instance: str
    way: _PageWay


class _Latest(BaseModel, frozen=True, extra="forbid"):
    """Each run's newest chunk, by run id: what picking a run shows, with nothing read."""

    runs: dict[str, Shown]


def _instance_id(parameters: ConsoleChartSettings, chart_id: str) -> str:
    """One per chart and settings, as a Plotly chart's: the editor's preview of a chart must not share its ids."""
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def latest_chunks(parameters: ConsoleChartSettings, dataframe: pd.DataFrame) -> dict[int, Shown]:
    """Each run's newest chunk, the one the panel read the URL of, by run id, in the order the frame lists the runs."""
    url_column = artifact_column(parameters.key)
    if dataframe.empty or url_column not in dataframe.columns:
        return {}
    tags_column = artifact_tags_column(parameters.key)
    newest = dataframe[dataframe[url_column].notna()].sort_values(MetricColumn.STEP)
    newest = newest.drop_duplicates(MetricColumn.RUN_ID, keep="last")
    order = list(dict.fromkeys(int(run) for run in dataframe[MetricColumn.RUN_ID]))
    found = {
        int(row[MetricColumn.RUN_ID]): Shown(
            experiment_id=int(row[EXPERIMENT_COLUMN]),
            run_id=int(row[MetricColumn.RUN_ID]),
            key=parameters.key,
            step=int(row[MetricColumn.STEP]),
            url=str(row[url_column]),
            tags=dict(parse_tags(row[tags_column])) if tags_column in newest.columns else {},
        )
        for _, row in newest.iterrows()
    }
    return {run: found[run] for run in order if run in found}


def _run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    named = dataframe.drop_duplicates(MetricColumn.RUN_ID)[[MetricColumn.RUN_ID, RUN_NAME_COLUMN]].dropna()
    return {int(run): str(name) for run, name in named.itertuples(index=False)}


def neighbour(store: DataStore[...], shown: Shown, way: _PageWay) -> Shown | None:
    """The chunk just before (`OLDER`) or after the one on show, of its run, or `None` if there is none nearby."""
    match way:
        case _PageWay.OLDER:
            span = StepRange(max(0, shown.step - SEEK_STEPS), shown.step - 1)
        case _PageWay.NEWER:
            span = StepRange(shown.step + 1, shown.step + SEEK_STEPS)
    if span.last < span.first:
        return None
    found = {
        artifact.step: artifact
        for artifact in store.fetch_artifacts(
            shown.experiment_id, keys=frozenset({shown.key}), run_ids=frozenset({shown.run_id}), steps=span
        )
        if artifact.step is not None
    }
    if not found:
        return None
    step = max(found) if way is _PageWay.OLDER else min(found)
    url = str(build_artifacts_dataframe([found[step]])[artifact_column(shown.key)].iloc[0])
    return shown.model_copy(update={"step": step, "url": url, "tags": dict(found[step].tags)})


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
        latest = latest_chunks(parameters, dataframe)
        if not latest:
            return dmc.Stack([dmc.Text("Nothing was printed in the runs shown", c="dimmed")])
        instance = _instance_id(parameters, context.chart_id)
        names = _run_names(dataframe)
        first = next(iter(latest.values()))

        def page(way: _PageWay, label: str) -> dmc.Button:
            return dmc.Button(
                label, id={"type": _PAGE, "instance": instance, "way": way}, size="xs", variant="default"
            )

        return dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Select(
                            id={"type": _RUN, "instance": instance},
                            data=[
                                {"value": str(run), "label": names.get(run, f"Run {run}")} for run in latest
                            ],
                            value=str(first.run_id),
                            allowDeselect=False,
                            size="xs",
                            w=200,
                        ),
                        page(_PageWay.OLDER, "Older"),
                        page(_PageWay.NEWER, "Newer"),
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
                html.Div(
                    id={"type": _LINES, "instance": instance},
                    className="dl-console",
                    style={"height": parameters.height},
                ),
                dcc.Store(id={"type": _SHOWN, "instance": instance}, data=first.model_dump()),
                dcc.Store(
                    id={"type": _LATEST, "instance": instance},
                    data=_Latest(runs={str(run): shown for run, shown in latest.items()}).model_dump(),
                ),
            ],
            gap="xs",
            w=parameters.width,
        )

    @typing.override
    @classmethod
    def data_request(cls, parameters: ConsoleChartSettings) -> DataRequest:
        # Every step a run logged is listed, but only its newest chunk's URL read: the rest are read one at a time.
        return DataRequest(
            artifacts=frozenset({parameters.key}),
            artifact_windows=frozenset({ArtifactWindow(parameters.key, 1, at=WindowAt.LATEST)}),
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
        dash.State({"type": _LATEST, "instance": dash.MATCH}, "data"),
        prevent_initial_call=True,
    )
    def show_run(run: str | None, latest: dict[str, Any]) -> dict[str, Any]:
        """A run picked: its newest chunk, which the chart already has."""
        runs = _Latest.model_validate(latest).runs
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
        """Older or Newer: the chunk beside the one on show, of its run."""
        on_show = Shown.model_validate(shown)
        del clicks
        found = neighbour(get_data_store(), on_show, require_triggered_id(_PageId).way)
        if found is None:
            raise PreventUpdate
        return found.model_dump()

    app.clientside_callback(
        _JS.source,
        dash.Output({"type": _LINES, "instance": dash.MATCH}, "children"),
        dash.Input({"type": _SHOWN, "instance": dash.MATCH}, "data"),
        dash.Input({"type": _FILTER, "instance": dash.MATCH}, "value"),
    )
