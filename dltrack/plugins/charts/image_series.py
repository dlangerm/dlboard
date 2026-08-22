"""An image chart with a step scrubber, w&b-style. One column per run, paginated, shared step slider."""

from __future__ import annotations

import hashlib
import json
import typing

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import dcc, html
from pydantic import BaseModel
from structlog.stdlib import get_logger

from dltrack.models import ChartType, ColumnKind

PAGE_SIZE = 6
GRID_COLS = 3

_log = get_logger(__name__)


class ImageChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Image chart settings."""

    key: str
    x_axis: str = "step"
    height: int = 220


def _escape_ref(ref: str) -> str:
    return ref.replace("/", "=").replace(":", "+")


def _instance_id(parameters: ImageChartSettings) -> str:
    """Stable id for pattern-matching Dash IDs. See earlier caveat re: collisions on identical configs."""
    raw = json.dumps(parameters.model_dump(), sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def _format_caption(raw_tags: str) -> str:
    try:
        tags = typing.cast("dict[str,str]", json.loads(raw_tags))
    except (TypeError, ValueError):
        return ""
    return ", ".join(f"{k}: {v}" for k, v in tags.items())


class ImageChart(ChartType[ImageChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """Scroll through images logged at each step, one column per run, paginated."""

    name: typing.ClassVar[str] = "image"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[ImageChartSettings]:
        return ImageChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: ImageChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        col = parameters.key
        x_col = parameters.x_axis
        if col not in dataframe.columns:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{col}'", c="dimmed")])

        tag_col = f"{col}__tags"
        has_tags = tag_col in dataframe.columns
        select_cols = ["run_id", x_col, col, *([tag_col] if has_tags else [])]

        df = (
            dataframe.loc[dataframe[col].notna(), select_cols]
            .drop_duplicates(subset=["run_id", x_col])
            .sort_values(["run_id", x_col])
        )
        if df.empty:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{col}'", c="dimmed")])

        run_ids = sorted(df["run_id"].unique().tolist())
        all_steps = sorted(df[x_col].unique().tolist())

        per_run_steps: dict[str, list[int]] = {}
        per_run_urls: dict[str, dict[str, str]] = {}
        per_run_captions: dict[str, dict[str, str]] = {}
        for rid, g in df.groupby("run_id"):
            steps = g[x_col].tolist()
            per_run_steps[str(rid)] = steps
            per_run_urls[str(rid)] = {
                str(s): f"/artifact/{_escape_ref(str(ref))}" for s, ref in zip(steps, g[col], strict=True)
            }
            if has_tags:
                per_run_captions[str(rid)] = {
                    str(s): _format_caption(raw) for s, raw in zip(steps, g[tag_col], strict=True)
                }

        inst = _instance_id(parameters)
        pages = [run_ids[i : i + PAGE_SIZE] for i in range(0, len(run_ids), PAGE_SIZE)]

        def _run_block(rid: int) -> dmc.Stack:
            first_step = str(per_run_steps[str(rid)][0])
            children = [
                dmc.Text(f"Run {rid}", size="sm", fw=600),
                dmc.Image(
                    id={"type": "image-series-img", "instance": inst, "run": str(rid)},
                    src=per_run_urls[str(rid)][first_step],
                    h=parameters.height,
                    fit="contain",
                ),
            ]
            if has_tags:
                children.append(
                    dmc.Text(
                        id={"type": "image-series-caption", "instance": inst, "run": str(rid)},
                        children=per_run_captions[str(rid)].get(first_step, ""),
                        size="xs",
                        c="dimmed",
                    )
                )
            return dmc.Stack(children, gap="xs")

        page_grids = [
            dmc.SimpleGrid(
                id={"type": "image-series-page", "instance": inst, "page": page_idx},
                cols=GRID_COLS,
                style={} if page_idx == 0 else {"display": "none"},
                children=[_run_block(rid) for rid in page_runs],
            )
            for page_idx, page_runs in enumerate(pages)
        ]

        pager = (
            dmc.Pagination(
                id={"type": "image-series-pager", "instance": inst},
                total=len(pages),
                value=1,
                mt="sm",
            )
            if len(pages) > 1
            else html.Div()
        )

        return dmc.Stack(
            [
                dcc.Store(
                    id={"type": "image-series-data", "instance": inst},
                    data={
                        "per_run_steps": per_run_steps,
                        "per_run_urls": per_run_urls,
                        "per_run_captions": per_run_captions,
                    },
                ),
                dmc.Slider(
                    id={"type": "image-series-slider", "instance": inst},
                    min=all_steps[0],
                    max=all_steps[-1],
                    value=all_steps[0],
                    step=1,
                    marks=[{"value": s, "label": str(s)} for s in all_steps],
                    persistence=True,
                    persistence_type="session",
                    persisted_props=["value"],
                ),
                *page_grids,
                pager,
            ],
            gap="sm",
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: ImageChartSettings) -> set[str]:
        return set() if parameters.x_axis == "step" else {parameters.x_axis}

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: ImageChartSettings) -> set[str]:
        return {parameters.key}

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT, "x_axis": ColumnKind.METRIC}


ImageChart.register(allow_override=True)


def plug(app: dash.Dash) -> None:
    """Register clientside callbacks for image-series scrubbing, captions, and pagination."""
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        """
        function(step, data) {
            if (!data) {
                return [window.dash_clientside.no_update, window.dash_clientside.no_update];
            }
            const runIds = Object.keys(data.per_run_steps);
            const srcs = [];
            const captions = [];
            runIds.forEach(function(rid) {
                const steps = data.per_run_steps[rid];
                let chosen = steps[0];
                for (let i = 0; i < steps.length; i++) {
                    if (steps[i] <= step) { chosen = steps[i]; } else { break; }
                }
                srcs.push(data.per_run_urls[rid][String(chosen)]);
                const runCaptions = data.per_run_captions ? data.per_run_captions[rid] : null;
                captions.push(runCaptions ? (runCaptions[String(chosen)] || "") : "");
            });
            return [srcs, captions];
        }
        """,
        [
            dash.Output({"type": "image-series-img", "instance": dash.MATCH, "run": dash.ALL}, "src"),
            dash.Output(
                {"type": "image-series-caption", "instance": dash.MATCH, "run": dash.ALL}, "children"
            ),
        ],
        dash.Input({"type": "image-series-slider", "instance": dash.MATCH}, "value"),
        dash.State({"type": "image-series-data", "instance": dash.MATCH}, "data"),
    )

    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        """
        function(page, ids) {
            return ids.map(function(id) {
                return id.page === (page - 1) ? {} : {display: "none"};
            });
        }
        """,
        dash.Output({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "style"),
        dash.Input({"type": "image-series-pager", "instance": dash.MATCH}, "value"),
        dash.State({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "id"),
    )
