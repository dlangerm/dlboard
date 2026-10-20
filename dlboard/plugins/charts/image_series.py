"""An image chart with a step scrubber, w&b-style. One column per run, paginated, shared step slider."""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import Any, cast

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field

from dlboard.client.artifacts.overlay import DEFAULT_CLASS_COLORS, DEFAULT_MASK_OPACITY
from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    DEFAULT_TAG_PRECISION,
    RUN_NAME_COLUMN,
    Artifact,
    ChartType,
    ColumnKind,
    LayerKind,
    RenderContext,
    SizeUnit,
    TagDisplay,
    TimeUnit,
    artifact_column,
    artifact_display_name,
    artifact_fname_column,
    artifact_tags_column,
    is_inlineable_artifact,
    layer_kind,
)
from dlboard.plugins.charts._artifact_tags import format_tags_caption, parse_tags
from dlboard.serve import AssetKind, ClientsideScript, series_swatch_class, serve_asset

if typing.TYPE_CHECKING:
    from collections.abc import Hashable

MAX_SLIDER_LABELS = 10


class OverlayLayer(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """How one layer is drawn over each picture."""

    opacity: float = Field(default=1.0, ge=0, le=1)
    """How opaque the layer is, from 0 (invisible) to 1 (the default)."""


class DrawnLayer(OverlayLayer):
    """A layer to draw over one picture, as the browser is told it: which file it is, and how to draw it."""

    url: str
    kind: LayerKind


class ImageChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Image chart settings."""

    key: str
    x_axis: str = "step"
    height: int = 220
    width: int | None = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height (the default). Unrelated to native_size below, which only affects individual thumbnails."""
    native_size: bool = Field(default=False)
    """Show each thumbnail at its logged resolution (capped by height, never upscaled) instead of stretching small images to fill the thumbnail box. Only affects individual thumbnails, not the chart's overall width above."""
    tag_precision: int = Field(default=DEFAULT_TAG_PRECISION, ge=1, le=15)
    """Significant digits the numbers in an image's caption (its tags) are shown with."""
    tag_time_unit: TimeUnit | None = Field(default=None)
    """Show tags logged as a duration in this unit, whatever unit they were logged in. Leave blank to pick the largest unit that fits each one."""
    tag_size_unit: SizeUnit | None = Field(default=None)
    """Show tags logged as a data size in this unit, whatever unit they were logged in. Leave blank to pick the largest unit that fits each one."""
    # The two tables come last: in the chart editor each takes a whole row, so the small settings above share theirs.
    overlays: dict[str, OverlayLayer] = Field(default_factory=dict)
    """
    The artifacts drawn over each picture, a row per artifact key, first row at the bottom. Each is the one the run logged at the picture's own step: an Overlay of shapes (see `dlboard.client.artifacts.overlay`), or an image such as a mask. A picture a row has nothing for is shown without it. Auto-generate adds the picture's own overlays, if there are any.
    """
    tag_display: dict[str, TagDisplay] = Field(default_factory=dict)
    """Display settings for particular tags, a row per tag name, taking the place of the chart's tag precision and unit settings for that tag. Leave a cell blank to keep the chart's own setting."""

    @property
    def tag_defaults(self) -> TagDisplay:
        """How every tag is shown unless `tag_display` names it."""
        return TagDisplay(
            precision=self.tag_precision, time_unit=self.tag_time_unit, size_unit=self.tag_size_unit
        )


def _instance_id(parameters: ImageChartSettings, chart_id: str) -> str:
    """
    Stable id for pattern-matching Dash IDs, one per chart *and* per settings.

    The chart's own id is part of it, not just its settings: the editor's preview renders the chart with the very
    settings it is about to be saved with, and two charts of one config are otherwise the same ids twice. Dash
    cannot resolve a `MATCH` callback with two components under one id, so each would stop responding.
    """
    raw = json.dumps([chart_id, parameters.model_dump()], sort_keys=True)
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


def _layers_element(element_id: dict[str, str]) -> html.Div:
    """The element over a picture that the browser draws its layers into (`image_series_overlay.js`)."""
    return html.Div(id=element_id, className="dl-image-series-overlay")


def _drawn_layers(row: dict[Hashable, Any], layers: dict[str, OverlayLayer]) -> list[DrawnLayer]:
    """The layers a picture's `row` of the panel dataframe has a file for, in the settings' order."""
    drawn: list[DrawnLayer] = []
    for key, layer in layers.items():
        url, fname = row.get(artifact_column(key)), row.get(artifact_fname_column(key))
        # A layer the run did not log at this step holds NaN, which is no string.
        if isinstance(url, str) and isinstance(fname, str):
            kind = layer_kind(parse_tags(row.get(artifact_tags_column(key))), fname)
            if kind is not None:
                drawn.append(DrawnLayer(url=url, kind=kind, opacity=layer.opacity))
    return drawn


def _layers_by_picture(
    pictures: pd.DataFrame, picture_col: str, layers: dict[str, OverlayLayer]
) -> dict[str, list[DrawnLayer]]:
    """Each picture's layers, by its URL: only the pictures with a layer at their own step, the rest are left out."""
    if not layers:
        return {}
    drawn = ((row[picture_col], _drawn_layers(row, layers)) for row in pictures.to_dict("records"))
    return {url: layers_of for url, layers_of in drawn if layers_of}


class ImageChart(ChartType[ImageChartSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """Scroll through images logged at each step, one column per run, paginated."""

    name: typing.ClassVar[str] = "image"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[ImageChartSettings]:
        return ImageChartSettings

    @classmethod
    @typing.override
    def can_display_artifact(cls, artifact: Artifact) -> bool:
        """Images a browser shows inline (PNG, JPEG, GIF, WebP)."""
        return is_inlineable_artifact(artifact.fname)

    @classmethod
    @typing.override
    def render(cls, parameters: ImageChartSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: ImageChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dmc.Stack:
        col = artifact_column(parameters.key)
        x_col = parameters.x_axis
        if col not in dataframe.columns:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        tag_col = artifact_tags_column(parameters.key)
        has_tags = tag_col in dataframe.columns
        layer_cols = [
            c
            for key in parameters.overlays
            for c in (artifact_column(key), artifact_fname_column(key), artifact_tags_column(key))
            if c in dataframe.columns
        ]
        has_names = RUN_NAME_COLUMN in dataframe.columns
        select_cols = [
            "run_id",
            x_col,
            col,
            *([tag_col] if has_tags else []),
            *layer_cols,
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
        # Each picture's layers, by the picture's URL: the modal only knows the URL it was opened on.
        overlays: dict[str, list[DrawnLayer]] = {}
        for rid, g in df.groupby("run_id"):
            steps = g[x_col].tolist()
            per_run_steps[str(rid)] = steps
            per_run_urls[str(rid)] = dict(zip((str(s) for s in steps), g[col], strict=True))
            if has_tags:
                per_run_captions[str(rid)] = {
                    str(s): format_tags_caption(raw, parameters.tag_defaults, parameters.tag_display)
                    for s, raw in zip(steps, g[tag_col], strict=True)
                }
            overlays.update(_layers_by_picture(g, col, parameters.overlays))
            if has_names:
                per_run_names[str(rid)] = g[RUN_NAME_COLUMN].iloc[0]

        inst = _instance_id(parameters, context.chart_id)
        pages = [run_ids[i : i + context.page_size] for i in range(0, len(run_ids), context.page_size)]

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
                    className=series_swatch_class(int(rid)),  # pyrefly: ignore [unnecessary-type-conversion]
                ),
                html.Div(
                    [
                        image,
                        _layers_element({"type": "image-series-overlay", "instance": inst, "run": str(rid)}),
                    ],
                    id={"type": "image-series-thumb", "instance": inst, "run": str(rid)},
                    className="dl-image-series-thumb",
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
                # No more columns than runs, so a lone run's image gets the chart's whole width, not a third.
                cols=min(context.columns, len(page_runs)),
                # The panel's column count moving applies to this at once, like the panel's own grid
                # (`panel_grid_followers.js`), rather than waiting for a rebuild.
                **cast("dict[str, Any]", {"data-dl-follow-panel-columns": "1"}),
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
                dmc.Text(artifact_display_name(parameters.key), fw=700, style={"overflowWrap": "anywhere"}),
                dcc.Store(
                    id={"type": "image-series-data", "instance": inst},
                    data={
                        "per_run_steps": per_run_steps,
                        "per_run_urls": per_run_urls,
                        "per_run_captions": per_run_captions,
                        "overlays": {
                            url: [layer.model_dump(mode="json") for layer in drawn]
                            for url, drawn in overlays.items()
                        },
                    },
                ),
                dmc.Slider(
                    id={"type": "image-series-slider", "instance": inst},
                    min=all_steps[0],
                    max=all_steps[-1],
                    value=all_steps[0],
                    marks=_slider_marks(all_steps),  # pyrefly: ignore [bad-argument-type]
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
                    # Kept in the page while closed, so the browser can draw the picture's overlay into it at once.
                    keepMounted=True,
                    children=html.Div(
                        [
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
                            _layers_element({"type": "image-series-modal-overlay", "instance": inst}),
                        ],
                        style={"display": "flex", "justifyContent": "center", "position": "relative"},
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
        # The layers' keys too: they are charted here, so auto-generate does not give them charts of their own.
        return {parameters.key, *parameters.overlays}

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: ImageChartSettings) -> set[str]:
        return set()

    @classmethod
    @typing.override
    def natural_width(cls, parameters: ImageChartSettings) -> int:
        return cls.natural_width_in(parameters, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def natural_width_in(cls, parameters: ImageChartSettings, context: RenderContext) -> int:
        return parameters.width or context.columns * (parameters.height + 40)

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT, "overlays": ColumnKind.LAYER, "x_axis": ColumnKind.METRIC}


ImageChart.register(allow_override=True)


_SCRUB_JS = ClientsideScript(Path(__file__).with_name("image_series_scrub.js"))
_PAGE_JS = ClientsideScript(Path(__file__).with_name("image_series_page.js"))
_LOCK_JS = ClientsideScript(Path(__file__).with_name("image_series_lock.js"))
_MODAL_OVERLAY_JS = ClientsideScript(Path(__file__).with_name("image_series_modal_overlay.js"))
_CSS_PATH = Path(__file__).with_name("image_series.css")
_OVERLAY_HELPER_PATH = Path(__file__).with_name("image_series_overlay.js")
"""Draws an overlay into a picture's layer; both the scrub and the modal call it, so it is a shared script."""


def _overlay_helper_script() -> bytes:
    """`image_series_overlay.js`, after the mask defaults the server has, so a mask is drawn in the palette Python uses."""
    defaults = {
        "DEFAULT_CLASS_COLORS": list(DEFAULT_CLASS_COLORS),
        "DEFAULT_MASK_OPACITY": DEFAULT_MASK_OPACITY,
    }
    return f"window.dlMaskDefaults = {json.dumps(defaults)};\n".encode() + _OVERLAY_HELPER_PATH.read_bytes()


def plug(app: dash.Dash) -> None:
    """Register clientside callbacks for image-series scrubbing, captions, pagination and overlays."""
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
    serve_asset(app, AssetKind.SCRIPT, _OVERLAY_HELPER_PATH.name, _overlay_helper_script())
    app.clientside_callback(
        _MODAL_OVERLAY_JS.source,
        dash.Output({"type": "image-series-modal-overlay", "instance": dash.MATCH}, "style"),
        dash.Input({"type": "image-series-modal", "instance": dash.MATCH}, "opened"),
        dash.State({"type": "image-series-modal-img", "instance": dash.MATCH}, "src"),
        dash.State({"type": "image-series-data", "instance": dash.MATCH}, "data"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
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

    # Moves every other slider in the same synced panel (`data-dl-panel-sync`) along with the one dragged.
    app.clientside_callback(
        _LOCK_JS.source,
        dash.Input({"type": "image-series-slider", "instance": dash.ALL}, "value"),
        dash.State({"type": "image-series-slider", "instance": dash.ALL}, "id"),
        dash.State({"type": "image-series-slider", "instance": dash.ALL}, "marks"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        _PAGE_JS.source,
        dash.Output({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "style"),
        dash.Input({"type": "image-series-pager", "instance": dash.MATCH}, "value"),
        dash.State({"type": "image-series-page", "instance": dash.MATCH, "page": dash.ALL}, "id"),
    )

    @app.callback(
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
        triggered_id = cast("dict[str, str] | None", ctx.triggered_id)
        if not triggered_id or not any(n_clicks_list):
            raise PreventUpdate
        triggered_run = triggered_id["run"]
        for src, img_id in zip(srcs, ids, strict=True):
            if img_id["run"] == triggered_run:
                return True, src
        raise PreventUpdate
