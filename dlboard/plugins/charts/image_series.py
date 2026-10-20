"""An image chart with a step scrubber, w&b-style. One column per run, paginated, shared step slider."""

from __future__ import annotations

import hashlib
import json
import typing
from pathlib import Path
from typing import Annotated, Any, cast

import dash
import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field, model_validator

from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    DEFAULT_TAG_PRECISION,
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    Artifact,
    ChartType,
    ColumnKind,
    LayerKind,
    OverlayKey,
    RenderContext,
    SettingsSection,
    SizeUnit,
    TagDisplay,
    TimeUnit,
    artifact_column,
    artifact_display_name,
    artifact_fname_column,
    artifact_keys_in,
    artifact_tags_column,
    is_inlineable_artifact,
    layer_kind,
    overlay_key,
)
from dlboard.plugins.charts._artifact_tags import format_tags_caption, parse_tags
from dlboard.serve import AssetKind, ClientsideScript, series_swatch_class, serve_asset

if typing.TYPE_CHECKING:
    from collections.abc import Hashable, Iterable

MAX_SLIDER_LABELS = 10


class LayerStyle(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """How one layer is drawn over each picture."""

    hidden: bool = False
    """Leave the layer out of the pictures."""
    opacity: float = Field(default=1.0, ge=0, le=1)
    """How opaque the layer is, from 0 (invisible) to 1 (the default)."""


class DrawnLayer(BaseModel, frozen=True):
    """A layer to draw over one picture, as the browser is told it: which file it is, and what kind of layer."""

    url: str
    kind: LayerKind
    layer: str
    """The name of the layer (`_layer_keys`), which its style, and a viewer's change, is looked up by."""


class ViewedLayer(BaseModel, frozen=True, extra="forbid"):
    """What a viewer changed about one layer."""

    hidden: bool


class ImageViewerState(BaseModel, frozen=True, extra="forbid"):
    """What a viewer changed in an image chart without editing it."""

    layers: dict[str, ViewedLayer] = Field(default_factory=dict)
    """Which layers they showed or hid, by name: the only part that can be saved as the chart's own."""
    overlays_hidden: bool = False
    """Every overlay is hidden (or peeked past): the viewer's own, never saved as the chart's."""
    legend_collapsed: bool = False
    """Only the legend's title is shown, to see more of the picture: the viewer's own, never saved either."""


class _LegacyOverlays(BaseModel, frozen=True):
    """The `overlays` setting a chart was saved with before every layer logged over a picture was drawn."""

    overlays: dict[str, LayerStyle]

    def as_settings(self, image_key: object) -> dict[str, object]:
        """
        The current settings that draw the same layers.

        The picture's own overlays are drawn anyway, so only a style that differs is kept, by the overlay's name; any
        other artifact is an extra layer.
        """
        extra_layers: list[str] = []
        layers: dict[str, LayerStyle] = {}
        for key, style in self.overlays.items():
            overlay = OverlayKey.parse(key)
            if overlay is not None and overlay.image_key == image_key:
                name = overlay.label
            else:
                extra_layers.append(key)
                name = key
            if style != LayerStyle():
                layers[name] = style
        return {"extra_layers": extra_layers, "layers": layers}


LAYERS_SECTION = SettingsSection("Layers")
"""What is drawn over each picture."""
TAGS_SECTION = SettingsSection("Tags")
"""How the tags under each picture are shown."""


class ImageChartSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Image chart settings."""

    key: str
    x_axis: str = "step"
    height: Annotated[int, DISPLAY_SECTION] = 220
    width: Annotated[int | None, DISPLAY_SECTION] = Field(default=None)
    """Overall chart width in px. Leave blank to size it automatically from height (the default). Unrelated to native_size below, which only affects individual thumbnails."""
    native_size: Annotated[bool, DISPLAY_SECTION] = Field(default=False)
    """Show each thumbnail at its logged resolution (capped by height, never upscaled) instead of stretching small images to fill the thumbnail box. Only affects individual thumbnails, not the chart's overall width above."""
    extra_layers: Annotated[list[str], LAYERS_SECTION] = Field(default_factory=list)
    """Other artifacts to draw over each picture, above its overlays and first at the bottom: an Overlay of shapes (see `dlboard.client.artifacts.overlay`) logged under a key of its own, or an image such as a mask. Each is the one the run logged at the picture's own step."""
    layers: Annotated[dict[str, LayerStyle], LAYERS_SECTION] = Field(default_factory=dict)
    """How particular layers are drawn, a row per layer: an overlay's name, or the key of an extra layer. A layer with no row is drawn in full. Every overlay logged over the picture is drawn, including ones logged later."""
    tag_precision: Annotated[int, TAGS_SECTION] = Field(default=DEFAULT_TAG_PRECISION, ge=1, le=15)
    """Significant digits the numbers in an image's caption (its tags) are shown with."""
    tag_time_unit: Annotated[TimeUnit | None, TAGS_SECTION] = Field(default=None)
    """Show tags logged as a duration in this unit, whatever unit they were logged in. Leave blank to pick the largest unit that fits each one."""
    tag_size_unit: Annotated[SizeUnit | None, TAGS_SECTION] = Field(default=None)
    """Show tags logged as a data size in this unit, whatever unit they were logged in. Leave blank to pick the largest unit that fits each one."""
    tag_display: Annotated[dict[str, TagDisplay], TAGS_SECTION] = Field(default_factory=dict)
    """Display settings for particular tags, a row per tag name, taking the place of the chart's tag precision and unit settings for that tag. Leave a cell blank to keep the chart's own setting."""

    @model_validator(mode="before")
    @classmethod
    def _from_legacy_overlays(cls, data: object) -> object:
        """A chart saved with an `overlays` row per layer loads as the `extra_layers` and `layers` that draw the same."""
        if not isinstance(data, dict):
            return data
        fields = typing.cast("dict[str, object]", data)
        if "overlays" not in fields:
            return data
        current = {key: value for key, value in fields.items() if key != "overlays"}
        migrated = _LegacyOverlays.model_validate({"overlays": fields["overlays"]}).as_settings(
            fields.get("key")
        )
        if current.keys() & migrated.keys():
            msg = "settings saved with `overlays` can't also set `extra_layers` or `layers`"
            raise ValueError(msg)
        return {**current, **migrated}

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
    return html.Div(
        id=element_id,
        className="dl-image-series-overlay",
        **cast("dict[str, Any]", {"data-dl-instance": element_id["instance"]}),
    )


def _drawn_layers(row: dict[Hashable, Any], layers: dict[str, str]) -> list[DrawnLayer]:
    """The layers a picture's `row` of the panel dataframe has a file for, in the order of `layers` (name to key)."""
    drawn: list[DrawnLayer] = []
    for name, key in layers.items():
        url, fname = row.get(artifact_column(key)), row.get(artifact_fname_column(key))
        # A layer the run did not log at this step holds NaN, which is no string.
        if isinstance(url, str) and isinstance(fname, str):
            kind = layer_kind(parse_tags(row.get(artifact_tags_column(key))), fname)
            if kind is not None:
                drawn.append(DrawnLayer(url=url, kind=kind, layer=name))
    return drawn


def _layer_keys(parameters: ImageChartSettings, columns: Iterable[str]) -> dict[str, str]:
    """
    The artifact key of each layer drawn over the pictures, by the layer's name, bottom first.

    The layers are every overlay logged over the picture (`overlay_key`; a layer logged after the chart was made is one
    too), then the `extra_layers`. A layer's name is its overlay's (`OverlayKey.label`), or for an extra layer its key: the
    name a `layers` row, and a viewer's change, is made under.
    """
    overlays = {
        overlay.label: key
        for key in sorted(artifact_keys_in(columns, overlay_key(parameters.key), prefix=True))
        if (overlay := OverlayKey.parse(key)) and overlay.image_key == parameters.key
    }
    return overlays | {key: key for key in parameters.extra_layers if key not in overlays}


def _legend_element(instance: str) -> html.Div:
    """
    The legend over one picture: its layers and what each holds, for the viewer to show and hide.

    Empty here. The browser fills it in (`image_series_overlay.js`) from this picture's own layers, since only it knows
    what the files hold (the classes of a mask, the labels of shapes) and which layers the picture has.
    """
    return html.Div(
        className="dl-layer-legend",
        **cast("dict[str, Any]", {"data-dl-instance": instance}),
    )


def _layers_by_picture(
    pictures: pd.DataFrame, picture_col: str, layers: dict[str, str]
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
        layers = _layer_keys(parameters, dataframe.columns)
        layer_cols = [
            c
            for key in layers.values()
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
            overlays.update(_layers_by_picture(g, col, layers))
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
                        *([_legend_element(inst)] if layers else []),
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

        # What each layer is when the viewer has changed nothing, by its name: the browser applies the viewer's changes over it.
        defaults = {
            name: {"hidden": style.hidden, "opacity": style.opacity}
            for name in layers
            if (style := parameters.layers.get(name, LayerStyle()))
        }
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
            className="dl-image-series",
            **cast(
                "dict[str, Any]",
                {
                    "data-dl-instance": inst,
                    "data-dl-view-key": context.viewer_state_key,
                    "data-dl-defaults": json.dumps(defaults),
                },
            ),
        )

    @classmethod
    @typing.override
    def viewer_state_type(cls) -> type[ImageViewerState]:
        return ImageViewerState

    @classmethod
    @typing.override
    def with_viewer_state(cls, parameters: ImageChartSettings, state: ImageViewerState) -> ImageChartSettings:
        """The chart with what the viewer changed about each layer made its own, keeping only what differs from a layer in full."""
        layers = dict(parameters.layers)
        for name, viewed in state.layers.items():
            layers[name] = layers.get(name, LayerStyle()).model_copy(update={"hidden": viewed.hidden})
        return parameters.model_copy(
            update={"layers": {n: s for n, s in layers.items() if s != LayerStyle()}}
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: ImageChartSettings) -> set[str]:
        return set() if parameters.x_axis == "step" else {parameters.x_axis}

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: ImageChartSettings) -> set[str]:
        # The extra layers' keys too: they are charted here, so auto-generate does not give them charts of their own.
        return {parameters.key, *parameters.extra_layers}

    @classmethod
    @typing.override
    def hint_required_artifact_key_prefixes(cls, parameters: ImageChartSettings) -> set[str]:
        # Every overlay logged over the picture, including ones logged after the chart was made.
        return {overlay_key(parameters.key)}

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
        return {
            "key": ColumnKind.ARTIFACT,
            "extra_layers": ColumnKind.LAYER,
            "layers": ColumnKind.LAYER_NAME,
            "x_axis": ColumnKind.METRIC,
        }


ImageChart.register(allow_override=True)


_SCRUB_JS = ClientsideScript(Path(__file__).with_name("image_series_scrub.js"))
_PAGE_JS = ClientsideScript(Path(__file__).with_name("image_series_page.js"))
_LOCK_JS = ClientsideScript(Path(__file__).with_name("image_series_lock.js"))
_MODAL_OVERLAY_JS = ClientsideScript(Path(__file__).with_name("image_series_modal_overlay.js"))
_CSS_PATH = Path(__file__).with_name("image_series.css")
_OVERLAY_HELPER_PATH = Path(__file__).with_name("image_series_overlay.js")
"""Draws an overlay into a picture's layer; both the scrub and the modal call it, so it is a shared script."""


def plug(app: dash.Dash) -> None:
    """Register clientside callbacks for image-series scrubbing, captions, pagination and overlays."""
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
    serve_asset(app, AssetKind.SCRIPT, _OVERLAY_HELPER_PATH.name, _OVERLAY_HELPER_PATH.read_bytes())
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
