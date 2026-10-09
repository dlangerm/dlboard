"""An image chart with a step scrubber, w&b-style. One column per run, paginated, shared step slider."""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import cast

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field
from structlog.stdlib import get_logger

from dlboard.models import RUN_NAME_COLUMN, ChartType, ColumnKind
from dlboard.plugins.charts._table_style import artifact_column, artifact_tags_column, format_tags_caption
from dlboard.serve import ClientsideScript, series_swatch_class

PAGE_SIZE = 6
GRID_COLS = 3
MAX_SLIDER_LABELS = 10

_log = get_logger(__name__)


class ImageChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Image chart settings."""

    key: str
    x_axis: str = "step"
    height: int = 220
    width: int | None = Field(
        default=None,
        description="Overall chart width in px. Leave blank to size it automatically from "
        "height (the default). Unrelated to native_size below, which only affects individual "
        "thumbnails.",
    )
    native_size: bool = Field(
        default=False,
        description="Show each thumbnail at its logged resolution (capped by height, never "
        "upscaled) instead of stretching small images to fill the thumbnail box. Only affects "
        "individual thumbnails, not the chart's overall width above.",
    )


def _instance_id(parameters: ImageChartSettings) -> str:
    """Stable id for pattern-matching Dash IDs. See earlier caveat re: collisions on identical configs."""
    raw = json.dumps(parameters.model_dump(), sort_keys=True)
    return hashlib.shake_256(raw.encode()).hexdigest(8)


def _slider_marks(all_steps: list[int], max_labels: int = MAX_SLIDER_LABELS) -> list[dict[str, typing.Any]]:
    """
    One mark per real step, but only label a sparse subset.

    Every real step gets a mark (so `restrictToMarks` only lets the slider stop where there's
    actually data), but only an evenly-spaced subset gets a text label — with many steps, labeling
    every mark makes them overlap into an unreadable smear.
    """
    if len(all_steps) <= max_labels:
        labeled = set(all_steps)
    else:
        stride = max(1, round(len(all_steps) / max_labels))
        labeled = set(all_steps[::stride])
        labeled.add(all_steps[-1])
    return [{"value": s, "label": str(s)} if s in labeled else {"value": s} for s in all_steps]


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
        col = artifact_column(parameters.key)
        x_col = parameters.x_axis
        if col not in dataframe.columns:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        tag_col = artifact_tags_column(parameters.key)
        has_tags = tag_col in dataframe.columns
        has_names = RUN_NAME_COLUMN in dataframe.columns
        select_cols = [
            "run_id",
            x_col,
            col,
            *([tag_col] if has_tags else []),
            *([RUN_NAME_COLUMN] if has_names else []),
        ]

        df = (
            dataframe.loc[dataframe[col].notna(), select_cols]
            .drop_duplicates(subset=["run_id", x_col])
            .sort_values(["run_id", x_col])
        )
        if df.empty:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        run_ids = sorted(df["run_id"].unique().tolist())
        all_steps = sorted(df[x_col].unique().tolist())

        per_run_steps: dict[str, list[int]] = {}
        per_run_urls: dict[str, dict[str, str]] = {}
        per_run_captions: dict[str, dict[str, str]] = {}
        per_run_names: dict[str, str] = {}
        for rid, g in df.groupby("run_id"):
            steps = g[x_col].tolist()
            per_run_steps[str(rid)] = steps
            per_run_urls[str(rid)] = dict(zip((str(s) for s in steps), g[col], strict=True))
            if has_tags:
                per_run_captions[str(rid)] = {
                    str(s): format_tags_caption(raw) for s, raw in zip(steps, g[tag_col], strict=True)
                }
            if has_names:
                per_run_names[str(rid)] = g[RUN_NAME_COLUMN].iloc[0]

        inst = _instance_id(parameters)
        pages = [run_ids[i : i + PAGE_SIZE] for i in range(0, len(run_ids), PAGE_SIZE)]

        # Small logged images (icons, tiny debug crops) get stretched to fill the thumbnail box
        # either way (`fit="contain"` upscales as well as downscales) -- "pixelated" keeps that
        # upscaling crisp/blocky instead of the default blurry interpolation.
        thumb_style = {
            "imageRendering": "pixelated",
            **(
                {
                    "maxHeight": f"{parameters.height}px",
                    "maxWidth": "100%",
                    "width": "auto",
                    "margin": "0 auto",
                }
                if parameters.native_size
                else {}
            ),
        }

        def _run_block(rid: int) -> dmc.Stack:
            first_step = str(per_run_steps[str(rid)][0])
            image = dmc.Image(
                id={"type": "image-series-img", "instance": inst, "run": str(rid)},
                src=per_run_urls[str(rid)][first_step],
                h=None if parameters.native_size else parameters.height,
                fit="contain",
                style=thumb_style,
            )
            children = [
                dmc.Text(
                    per_run_names.get(str(rid), f"Run {rid}"),
                    size="sm",
                    fw=600,
                    className=series_swatch_class(int(rid)),
                ),
                html.Div(
                    image,
                    id={"type": "image-series-thumb", "instance": inst, "run": str(rid)},
                    n_clicks=0,
                    style={"cursor": "zoom-in"},
                ),
            ]
            if has_tags:
                children.append(
                    dmc.Text(
                        id={"type": "image-series-caption", "instance": inst, "run": str(rid)},
                        children=per_run_captions[str(rid)].get(first_step, ""),
                        size="xs",
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
                    marks=_slider_marks(all_steps),  # pyright: ignore[reportArgumentType]
                    restrictToMarks=True,
                    persistence=True,
                    persistence_type="session",
                    persisted_props=["value"],
                    mb="xl",
                ),
                *page_grids,
                pager,
                dmc.Modal(
                    id={"type": "image-series-modal", "instance": inst},
                    opened=False,
                    size="90%",
                    padding=0,
                    centered=True,
                    children=html.Div(
                        dmc.Image(
                            id={"type": "image-series-modal-img", "instance": inst},
                            # fit="contain" within a fixed box scales a small image up (rather
                            # than showing it as a speck in an otherwise-empty modal) and a large
                            # one down to fit -- either way, the browser's own pinch/ctrl-scroll
                            # zoom still reveals further detail on top of this, no custom +/-
                            # control needed.
                            fit="contain",
                            h="80vh",
                            w="100%",
                            style={"imageRendering": "pixelated"},
                        ),
                        style={"display": "flex", "justifyContent": "center"},
                    ),
                ),
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
    def hint_required_hparams(cls, parameters: ImageChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def natural_width(cls, parameters: ImageChartSettings) -> int:
        return parameters.width or GRID_COLS * (parameters.height + 40)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT, "x_axis": ColumnKind.METRIC}


ImageChart.register(allow_override=True)


_SCRUB_JS = ClientsideScript(Path(__file__).with_name("image_series_scrub.js"))
_PAGE_JS = ClientsideScript(Path(__file__).with_name("image_series_page.js"))


def plug(app: dash.Dash) -> None:
    """Register clientside callbacks for image-series scrubbing, captions, and pagination."""
    app.clientside_callback(  # pyright: ignore[reportUnknownMemberType]
        _SCRUB_JS.source,
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
        _PAGE_JS.source,
        dash.Output({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "style"),
        dash.Input({"type": "image-series-pager", "instance": dash.MATCH}, "value"),
        dash.State({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "id"),
    )

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output({"type": "image-series-modal", "instance": MATCH}, "opened"),
        Output({"type": "image-series-modal-img", "instance": MATCH}, "src"),
        Input({"type": "image-series-thumb", "instance": MATCH, "run": ALL}, "n_clicks"),
        State({"type": "image-series-img", "instance": MATCH, "run": ALL}, "src"),
        State({"type": "image-series-img", "instance": MATCH, "run": ALL}, "id"),
        prevent_initial_call=True,
    )
    def open_image_modal(
        n_clicks_list: list[int], srcs: list[str], ids: list[dict[str, str]]
    ) -> tuple[bool, str]:
        triggered_id = cast("dict[str, str] | None", ctx.triggered_id)  # pyright: ignore[reportUnknownMemberType]
        if not triggered_id or not any(n_clicks_list):
            raise PreventUpdate
        triggered_run = triggered_id["run"]
        for src, img_id in zip(srcs, ids, strict=True):
            if img_id["run"] == triggered_run:
                return True, src
        raise PreventUpdate
