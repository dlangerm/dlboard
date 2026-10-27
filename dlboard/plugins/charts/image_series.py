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
from dash import ALL, MATCH, Input, Output, ctx, dcc, html
from dash.exceptions import PreventUpdate
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from dlboard._compat import StrEnum
from dlboard.models import (
    DEFAULT_RENDER_CONTEXT,
    DEFAULT_TAG_PRECISION,
    DISPLAY_SECTION,
    RUN_NAME_COLUMN,
    Artifact,
    ChartType,
    ColumnChoices,
    ColumnKind,
    DataRequest,
    FixedRows,
    OverlayKey,
    RenderContext,
    SettingsSection,
    SizeUnit,
    TagDisplay,
    TimeUnit,
    ViewerState,
    artifact_column,
    artifact_display_name,
    artifact_fname_column,
    artifact_tags_column,
    is_inlineable_artifact,
    overlay_key,
)
from dlboard.plugins.charts._artifact_tags import format_tags, format_tags_caption
from dlboard.plugins.charts._layers import (
    DrawnLayer,
    drawn_layers,
    is_overlay_of,
    overlay_keys_of,
    serve_layered_picture,
)
from dlboard.serve import AssetKind, ClientsideScript, series_swatch_class, serve_asset

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

MAX_SLIDER_LABELS = 10
DEFAULT_FIT_PIXELS = 1920
"""The longest side, in pixels, the enlarged view's "Fit to" download starts at: a full-HD frame's width."""


class LayerVisibility(StrEnum):
    """
    Whether a layer is drawn. Saved with a chart, so a value is never renamed (`persisted_enum_values_test`).

    A viewer can switch a layer between shown and hidden from the legend; one that is off is not theirs to bring back.
    """

    SHOWN = "shown"
    """Drawn over the pictures."""
    HIDDEN = "hidden"
    """Listed in the legend but not drawn until a viewer shows it."""
    OFF = "off"
    """Not drawn, not listed in the legend, and not fetched."""


class LayerStyle(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """How one layer is drawn over each picture."""

    visibility: LayerVisibility = LayerVisibility.SHOWN
    """`shown`; `hidden` (in the legend, drawn only when a viewer shows it); or `off` (not drawn, and not in the legend)."""
    opacity: float = Field(default=1.0, ge=0, le=1)
    """How opaque the layer is, from 0 (invisible) to 1 (the default)."""


def _layer_label(key: str, image_key: str) -> str:
    """What a layer is called where it is shown: an overlay of the picture by its name, any other artifact by its key."""
    overlay = OverlayKey.parse(key)
    return overlay.label if overlay is not None and overlay.image_key == image_key else key


LAYERS_SECTION = SettingsSection("Layers", opened=True)
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
    layers: Annotated[dict[str, LayerStyle], LAYERS_SECTION] = Field(default_factory=dict)
    """
    Every layer drawn over each picture, bottom first, a row per artifact key: the overlays logged over it, then any other artifact added by its key. A hidden layer is in the legend but drawn only when a viewer shows it; an off layer is neither. Overlays logged later are drawn too.
    """
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
    def _from_overlays(cls, data: object) -> object:
        """
        A chart saved with an `overlays` row per layer loads as the `layers` that draw the same.

        Rows saved in `layers` itself win over what is carried across.
        """
        if not isinstance(data, dict) or "overlays" not in data:
            return data
        fields = typing.cast("dict[str, object]", data)
        current = {key: value for key, value in fields.items() if key != "overlays"}
        carried = typing.cast("dict[str, object]", fields["overlays"])
        return {
            **current,
            "layers": {**carried, **typing.cast("dict[str, object]", current.get("layers", {}))},
        }

    @field_validator("layers")
    @classmethod
    def _without_overlays_drawn_anyway(
        cls, layers: dict[str, LayerStyle], info: ValidationInfo
    ) -> dict[str, LayerStyle]:
        """
        Leaves out a row for an overlay of the picture that is in every way as it would be drawn without one.

        Every overlay logged over the picture is drawn, so the editor lists them all, and saving what it lists would
        write a row per overlay; this keeps what is saved to the layers that are treated differently.
        """
        return {
            key: style
            for key, style in layers.items()
            if not is_overlay_of(key, info.data.get("key")) or style != LayerStyle()
        }

    @property
    def tag_defaults(self) -> TagDisplay:
        """How every tag is shown unless `tag_display` names it."""
        return TagDisplay(
            precision=self.tag_precision, time_unit=self.tag_time_unit, size_unit=self.tag_size_unit
        )


class ViewedLayer(BaseModel, frozen=True, extra="forbid"):
    """What a viewer changed about one layer."""

    hidden: bool


class ImageViewerState(ViewerState[ImageChartSettings], frozen=True, extra="forbid"):
    """What a viewer changed in an image chart without editing it: which layers they showed or hid."""

    layers: dict[str, ViewedLayer] = Field(default_factory=dict)
    """Which layers they showed or hid, by name."""

    @typing.override
    def applied_to(self, parameters: ImageChartSettings) -> ImageChartSettings:
        """The chart with each layer the viewer showed or hid set that way. A layer that is off is not theirs to change."""
        layers = dict(parameters.layers)
        for name, viewed in self.layers.items():
            style = layers.get(name, LayerStyle())
            if style.visibility is not LayerVisibility.OFF:
                visibility = LayerVisibility.HIDDEN if viewed.hidden else LayerVisibility.SHOWN
                layers[name] = style.model_copy(update={"visibility": visibility})
        return parameters.model_copy(update={"layers": layers})


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


def _layer_keys(parameters: ImageChartSettings, columns: Iterable[str]) -> list[str]:
    """
    The artifact key of each layer drawn over the pictures, bottom first.

    The layers are every overlay logged over the picture (`overlay_key`; one logged after the chart was made is a layer
    too), in key order, then the other artifacts the `layers` rows name, in row order. An off layer is none of them.
    """
    present = set(columns)
    overlays = overlay_keys_of(parameters.key, present)
    others = [key for key in parameters.layers if key not in overlays and artifact_column(key) in present]
    return [
        key
        for key in (*overlays, *others)
        if parameters.layers.get(key, LayerStyle()).visibility is not LayerVisibility.OFF
    ]


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


class DownloadSize(StrEnum):
    """How big the enlarged view's download is; the value is what the browser is told (`image_series_viewer.js`)."""

    ORIGINAL = "1"
    TWICE = "2"
    FOUR_TIMES = "4"
    EIGHT_TIMES = "8"
    FIT = "fit"
    """The picture's longest side at a number of pixels the viewer types (`DEFAULT_FIT_PIXELS` to begin with)."""
    VIEW = "view"
    """What is on screen: the zoomed and panned area, at the screen's resolution."""

    @property
    def label(self) -> str:
        """What the button for it says."""
        return {self.ORIGINAL: "Original", self.FIT: "Fit to", self.VIEW: "View"}.get(self, f"{self.value}x")

    @property
    def description(self) -> str:
        """What the button for it does, for its tooltip."""
        match self:
            case DownloadSize.ORIGINAL:
                return "Download the picture at its own size"
            case DownloadSize.TWICE | DownloadSize.FOUR_TIMES | DownloadSize.EIGHT_TIMES:
                return f"Download the picture magnified {self.value} times"
            case DownloadSize.FIT:
                return "Download the picture with its longest side at the number of pixels beside this"
            case DownloadSize.VIEW:
                return "Download what is on screen: the area zoomed and panned to"


def _view_button(class_name: str, text: str, title: str, **attributes: str) -> html.Button:
    """A button of the enlarged view, which the browser handles (`image_series_viewer.js`)."""
    return html.Button(
        text,
        className=class_name,
        title=title,
        type="button",
        **cast("dict[str, Any]", {"aria-label": title, **attributes}),
    )


def _download_choices() -> html.Span:
    """
    Download, which offers its sizes on being pointed at: the choice is the click, with no menu to open first.

    Each button downloads the picture with the layers shown and their legend, as a PNG. "Fit to" is followed by the
    number of pixels for the picture's longest side, which the viewer can type.
    """
    buttons = {
        size: _view_button(
            "dl-view-download-choice", size.label, size.description, **{"data-dl-download": size}
        )
        for size in DownloadSize
    }
    fit_to = html.Span(
        [
            buttons[DownloadSize.FIT],
            dmc.NumberInput(
                value=DEFAULT_FIT_PIXELS,
                min=1,
                step=1,
                allowDecimal=False,
                allowNegative=False,
                clampBehavior="strict",
                hideControls=True,
                size="xs",
                w=72,
                classNames={"input": "dl-view-download-pixels"},
                **{"aria-label": "Pixels along the picture's longest side"},
            ),
            "px",
        ],
        className="dl-view-download-fit",
    )
    return html.Span(
        [
            html.Span(
                [fit_to if size is DownloadSize.FIT else button for size, button in buttons.items()],
                className="dl-view-download-choices",
            ),
            html.Span("Download", className="dl-view-download-label", tabIndex=0),
        ],
        className="dl-view-download",
    )


def _enlarged_view(instance: str, key: str, *, has_legend: bool) -> dmc.Modal:
    """
    The enlarged view of a picture, opened by clicking a thumbnail.

    The title (the chart's, the run with its color, the step) and a download button are the modal's header; the body is the
    picture's tags as a table beside the picture, which is zoomed and panned, and a timeline to scrub along. The browser fills
    in what is particular to the picture on show, as it does the legend (`image_series_viewer.js`).
    """
    at = cast("dict[str, Any]", {"data-dl-instance": instance})
    return dmc.Modal(
        id={"type": "image-series-modal", "instance": instance},
        opened=False,
        size="90%",
        centered=True,
        # Kept in the page while closed, so the browser can draw the picture's overlay into it at once.
        keepMounted=True,
        title=html.Span(
            [
                html.Span(artifact_display_name(key), className="dl-view-chart"),
                html.Span(className="dl-view-run"),
                html.Span(className="dl-view-step"),
                _download_choices(),
            ],
            className="dl-view-title",
            **at,
        ),
        children=[
            html.Div(
                [
                    html.Div(className="dl-view-tags", **at),
                    html.Div(
                        [
                            # The picture and its layers, which the browser moves and scales together to zoom and pan.
                            # fit="contain" in a box of the viewer's size scales a small image up (rather than showing it
                            # as a speck) and a large one down to fit.
                            html.Div(
                                [
                                    dmc.Image(
                                        id={"type": "image-series-modal-img", "instance": instance},
                                        fit="contain",
                                        h="100%",
                                        w="100%",
                                        style={"imageRendering": "pixelated"},
                                    ),
                                    _layers_element(
                                        {"type": "image-series-modal-overlay", "instance": instance}
                                    ),
                                ],
                                className="dl-zoom-content",
                            ),
                            *([_legend_element(instance)] if has_legend else []),
                            html.Div(className="dl-zoom-controls"),
                            _view_button("dl-view-nav dl-view-prev", "<", "Previous step (left arrow)"),
                            _view_button("dl-view-nav dl-view-next", ">", "Next step (right arrow)"),
                        ],
                        className="dl-image-series-viewer",
                        **at,
                    ),
                ],
                className="dl-view-main",
            ),
            html.Div(className="dl-view-timeline", **at),
        ],
    )


def _layers_by_picture(
    pictures: pd.DataFrame, picture_col: str, layers: list[str]
) -> dict[str, list[DrawnLayer]]:
    """Each picture's layers, by its URL: only the pictures with a layer at their own step, the rest are left out."""
    if not layers:
        return {}
    drawn = ((row[picture_col], drawn_layers(row, layers)) for row in pictures.to_dict("records"))
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
            for key in layers
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
        per_run_tags: dict[str, dict[str, dict[str, str]]] = {}
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
                # The same tags a name and a value each, for the enlarged view's table.
                per_run_tags[str(rid)] = {
                    str(s): format_tags(raw, parameters.tag_defaults, parameters.tag_display)
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

        # What each layer is when the viewer has changed nothing, by its key, with the name the legend shows for it: the
        # browser applies the viewer's changes over it.
        defaults = {
            key: {
                "hidden": style.visibility is LayerVisibility.HIDDEN,
                "opacity": style.opacity,
                "label": _layer_label(key, parameters.key),
            }
            for key in layers
            if (style := parameters.layers.get(key, LayerStyle()))
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
                        "per_run_tags": per_run_tags,
                        "per_run_names": per_run_names,
                        # How the enlarged view marks each run with its color, as its thumbnail's name is.
                        "per_run_swatches": {str(rid): series_swatch_class(int(rid)) for rid in run_ids},
                        "x_axis": x_col,
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
                # Which run the enlarged view is on, set by the thumbnail clicked and by the keyboard.
                dcc.Store(id={"type": "image-series-modal-run", "instance": inst}, data=None),
                _enlarged_view(inst, parameters.key, has_legend=bool(layers)),
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
    def fixed_table_rows(cls, parameters: ImageChartSettings, choices: ColumnChoices) -> FixedRows:
        """Every overlay logged over the picture is a row of the Layers table, by its name: it is drawn whether or not it is saved."""
        overlays = sorted(key for key in choices.artifact_keys() if is_overlay_of(key, parameters.key))
        return {"layers": {key: _layer_label(key, parameters.key) for key in overlays}}

    @classmethod
    @typing.override
    def data_request(cls, parameters: ImageChartSettings) -> DataRequest:
        return DataRequest(
            metrics=frozenset() if parameters.x_axis == "step" else frozenset({parameters.x_axis}),
            # The keys the layers name too: they are charted here, so auto-generate does not give them charts of their own.
            artifacts=frozenset({parameters.key, *parameters.layers}),
            # Every overlay logged over the picture, including ones logged after the chart was made.
            artifact_prefixes=frozenset({overlay_key(parameters.key)}),
        )

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
            "layers": ColumnKind.LAYER,
            "x_axis": ColumnKind.METRIC,
        }


ImageChart.register(allow_override=True)


_SCRUB_JS = ClientsideScript(Path(__file__).with_name("image_series_scrub.js"))
_PAGE_JS = ClientsideScript(Path(__file__).with_name("image_series_page.js"))
_LOCK_JS = ClientsideScript(Path(__file__).with_name("image_series_lock.js"))
_MODAL_PICTURE_JS = ClientsideScript(Path(__file__).with_name("image_series_modal_picture.js"))
_MODAL_OVERLAY_JS = ClientsideScript(Path(__file__).with_name("image_series_modal_overlay.js"))
_CSS_PATH = Path(__file__).with_name("image_series.css")
_OVERLAY_HELPER_PATH = Path(__file__).with_name("image_series_overlay.js")
"""Draws an image chart's legend over its pictures; both the scrub and the modal call it, so it is a shared script."""
_VIEWER_HELPER_PATH = Path(__file__).with_name("image_series_viewer.js")
"""Zooms and pans the enlarged view and moves it with the keyboard; also finds a run's step, which the scrub shares."""


def plug(app: dash.Dash) -> None:
    """Register clientside callbacks for image-series scrubbing, captions, pagination and overlays."""
    serve_layered_picture(app)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
    serve_asset(app, AssetKind.SCRIPT, _OVERLAY_HELPER_PATH.name, _OVERLAY_HELPER_PATH.read_bytes())
    serve_asset(app, AssetKind.SCRIPT, _VIEWER_HELPER_PATH.name, _VIEWER_HELPER_PATH.read_bytes())
    # The enlarged view shows the run's picture at the slider's step, with its title, tags and timeline, while it is open. It sets
    # the picture's `src` itself, from the browser, once the picture has loaded (`image_series_viewer.js`).
    app.clientside_callback(
        _MODAL_PICTURE_JS.source,
        dash.Output({"type": "image-series-modal-img", "instance": dash.MATCH}, "src"),
        dash.Input({"type": "image-series-modal", "instance": dash.MATCH}, "opened"),
        dash.Input({"type": "image-series-modal-run", "instance": dash.MATCH}, "data"),
        dash.Input({"type": "image-series-slider", "instance": dash.MATCH}, "value"),
        dash.State({"type": "image-series-data", "instance": dash.MATCH}, "data"),
        prevent_initial_call=True,
    )
    app.clientside_callback(
        _MODAL_OVERLAY_JS.source,
        dash.Output({"type": "image-series-modal-overlay", "instance": dash.MATCH}, "style"),
        dash.Input({"type": "image-series-modal-img", "instance": dash.MATCH}, "src"),
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
        Output({"type": "image-series-modal-run", "instance": MATCH}, "data"),
        Input({"type": "image-series-thumb", "instance": MATCH, "run": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_image_modal(n_clicks_list: list[int]) -> tuple[bool, str]:
        triggered_id = cast("dict[str, str] | None", ctx.triggered_id)
        if not triggered_id or not any(n_clicks_list):
            raise PreventUpdate
        return True, triggered_id["run"]
