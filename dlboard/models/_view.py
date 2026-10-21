"""Data models for rendering an experiment view."""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import types
import typing
from abc import ABC, abstractmethod

from annotated_types import Ge, Le
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from structlog.stdlib import get_logger

from dlboard._compat import StrEnum

if typing.TYPE_CHECKING:
    from dlboard.models._artifact import Artifact

_log = get_logger(__name__)

# Pre-3.12 `TypeVar` style (the client's floor is 3.10). `_Parameters` is a chart type's own
# settings model; `_Dataframe`/`_Chart` are its input/output.
_Parameters = typing.TypeVar("_Parameters", bound=BaseModel)
_Dataframe = typing.TypeVar("_Dataframe")
_Chart = typing.TypeVar("_Chart")

_CHART_ID_LENGTH = 10

_FALLBACK_NATURAL_WIDTH: typing.Final = 400
"""Width used for a chart whose persisted `parameters` no longer validate (e.g. after a schema
change), so a single broken chart can't crash the whole panel's layout -- `render()` still raises
for the same case, surfacing as that one chart's own inline error instead."""


MIN_GRID_COLUMNS: typing.Final = 1
MAX_GRID_COLUMNS: typing.Final = 6
GridColumns = typing.Annotated[int, Field(ge=MIN_GRID_COLUMNS, le=MAX_GRID_COLUMNS)]
"""How many columns a panel's `"grid"` layout has -- the one range both the model and the UI use."""


class ColumnKind(StrEnum):
    """Registry of dataframe column kinds a chart parameter field can be populated from."""

    METRIC = "metric"
    ARTIFACT = "artifact"
    HISTOGRAM = "histogram"
    HPARAM = "hparam"
    LAYER = "layer"
    """Artifact keys an image chart can draw over a picture besides its own overlays: an `Overlay` logged under a key of its own, or an image. No dataframe column is ever tagged with this kind directly."""
    GROUPING = "grouping"
    """Synthetic kind for fields that group/split by any metric or hparam column, plus `run_id` --
    see `group_columns_by_kind`. No dataframe column is ever tagged with this kind directly."""


class RenderContext(BaseModel, frozen=True, extra="forbid"):
    """What the panel and the page around a chart say about how it is laid out, for a chart that follows them."""

    columns: GridColumns = 3
    """The panel's `grid_columns`: how many items a chart that lays out a grid of its own puts in a row."""

    page_size: int = Field(default=12, ge=1)
    """How many items a chart that pages its own contents shows at once: the viewer's charts-per-page size."""

    chart_id: str = ""
    """
    The chart's own `ChartInstance.id`: a stable handle for naming its components and keying UI state it keeps
    (a grid's sort and filters) across redraws. Empty for a chart rendered on its own.
    """

    viewer_state_key: str = ""
    """
    What a chart that keeps viewer state (`ChartType.viewer_state_type`) names that state by in the browser, so the
    page and the chart agree on where it is kept. Empty for a chart whose viewer state is not kept.
    """


DEFAULT_RENDER_CONTEXT: typing.Final = RenderContext()
"""For a chart rendered on its own, outside any panel or page (a test, a preview)."""


class ColumnChoices(typing.Protocol):
    """
    What an experiment has that a chart's settings can name, as a chart type needs to know it to show its settings.

    The chart editor's `ColumnCatalog` is one; it lives with the page, so a chart type sees it only through this.
    """

    def options(self, kind: ColumnKind) -> list[str]:
        """The names a setting of `kind` can take."""
        ...

    def artifact_keys(self) -> list[str]:
        """Every artifact key the experiment has logged, in key order."""
        ...


FixedRows = dict[str, dict[str, str]]
"""The rows a chart draws without saving them (`ChartType.fixed_table_rows`): for each table setting, a row's key and its label."""


class ParameterFieldType(StrEnum):
    """Widget type a chart parameter field should be rendered as."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    LIST_STR = "list[str]"
    LIST_PATTERN = "list[pattern]"
    """A `list[str]` of names, each of which may instead be a prefix ending in `*` (see `NamePatterns`)."""
    TABLE = "table"
    """A `dict[str, Model]`, edited as a table with a row per entry: its key, then a cell per field of `Model`."""


@dataclasses.dataclass(frozen=True)
class NamePatterns:
    """
    Marks a `list[str]` setting as taking names or `prefix*` patterns, not only names that exist.

    The editor offers the experiment's names as suggestions but lets anything be typed, which is how a
    setting can ask for every key under a prefix, including ones logged later.
    """


NamePatternList = typing.Annotated[list[str], NamePatterns()]
"""The type of a setting marked `NamePatterns`."""


@dataclasses.dataclass(frozen=True)
class SettingsSection:
    """
    A titled group of a chart type's settings in the chart editor.

    Mark a setting with one in its `Annotated` metadata (`height: Annotated[int, DISPLAY_SECTION] = 300`); a setting
    with none is in `DATA_SECTION`. The sections show in the order their first setting is declared, and a chart type
    whose settings are all in one section shows them with no heading. A chart type may declare sections of its own.
    """

    title: str
    opened: bool = False
    """Whether the editor shows the section expanded; the rest start collapsed."""


DATA_SECTION: typing.Final = SettingsSection("Data", opened=True)
"""What a chart draws: the metrics, keys and columns it is of. The default section."""
DISPLAY_SECTION: typing.Final = SettingsSection("Display")
"""How it looks: sizes, orientation, axes."""
ADVANCED_SECTION: typing.Final = SettingsSection("Advanced")
"""Settings that rarely need changing, like limits that keep a chart fast."""


class ViewerState(BaseModel, typing.Generic[_Parameters], frozen=True):
    """
    What a viewer changed in a chart without editing it: which layers they showed, a series they hid.

    A chart type with viewer state returns its model from `ChartType.viewer_state_type()`. The state is kept in the
    browser, and "Save as default" makes it the chart's own settings through `applied_to`.
    """

    @abstractmethod
    def applied_to(self, parameters: _Parameters) -> _Parameters:
        """`parameters` (the chart's settings) with this state made its own: what "Save as default" saves."""


class ChartType(ABC, BaseModel, typing.Generic[_Parameters, _Dataframe, _Chart], frozen=True, extra="forbid"):
    """A type of chart."""

    name: typing.ClassVar[str]
    """The name of the chart type."""

    @classmethod
    @abstractmethod
    def parameter_type(cls) -> type[_Parameters]:
        """The parameter container type."""

    @classmethod
    @abstractmethod
    def render(cls, parameters: _Parameters, dataframe: _Dataframe) -> _Chart:
        """Render a chart given an instance."""

    @classmethod
    def render_in(cls, parameters: _Parameters, dataframe: _Dataframe, context: RenderContext) -> _Chart:  # noqa: ARG003
        """Render a chart where `context` places it. A chart that does not follow its panel or page just renders."""
        return cls.render(parameters, dataframe)

    @classmethod
    @abstractmethod
    def hint_required_columns(cls, parameters: _Parameters) -> set[str] | None:
        """Hint at the columns required for this chart."""

    @classmethod
    @abstractmethod
    def hint_required_artifact_keys(cls, parameters: _Parameters) -> set[str] | None:
        """Hint at the columns required for this chart."""

    @classmethod
    def hint_required_artifact_key_prefixes(cls, parameters: _Parameters) -> set[str]:  # noqa: ARG003
        """
        Hint at artifact key prefixes this chart needs: every key that starts with one of them.

        For a chart that lists a whole family of artifacts (every `checkpoints/...` file) rather
        than one named key, so keys logged later are included without editing the chart.
        Defaults to none, so a chart type that only ever names its keys need not say anything.
        """
        return set()

    @classmethod
    def hint_required_histogram_keys(cls, parameters: _Parameters) -> set[str]:  # noqa: ARG003
        """
        Hint at the logged histogram keys this chart shows.

        Defaults to none, so a chart type that does not show histograms need not say anything. Unlike
        the metric hint there is no "everything": a histogram is the most expensive thing a panel fetches,
        so a chart always names the ones it draws.
        """
        return set()

    @classmethod
    @abstractmethod
    def hint_required_hparams(cls, parameters: _Parameters) -> set[str] | None:
        """Hint at the hyperparameter keys required for this chart."""

    @classmethod
    @abstractmethod
    def natural_width(cls, parameters: _Parameters) -> int:
        """Preferred render width in px, derived from this chart's own settings (e.g. height + aspect ratio)."""

    @classmethod
    def natural_width_in(cls, parameters: _Parameters, context: RenderContext) -> int:  # noqa: ARG003
        """`natural_width` where `context` places the chart, for one whose width depends on it."""
        return cls.natural_width(parameters)

    @classmethod
    def register(cls, *, allow_override: bool = False) -> None:
        ChartTypeRegistry.register(cls, allow_override=allow_override)

    @classmethod
    @abstractmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        """
        Map string parameter field names to the column kind that populates them.

        Fields left unlisted are not drawn from the experiment's columns: the chart editor gives them a plain text input.
        """

    @classmethod
    def can_display_artifact(cls, artifact: Artifact) -> bool:  # noqa: ARG003
        """
        Whether this chart type can display `artifact`.

        What auto-generating and suggesting charts go by to pick a chart for an artifact key: the first
        built-in chart type that says yes. An artifact no chart type claims (a histogram of gradients
        logged as a custom kind, say) simply gets no generated chart, rather than one that cannot show it.
        Defaults to `False`, so a chart type that does not display artifacts need not say anything.
        """
        return False

    @classmethod
    def fixed_table_rows(cls, parameters: _Parameters, choices: ColumnChoices) -> FixedRows:  # noqa: ARG003
        """
        The rows of the chart's table settings that the chart draws whether or not they are saved, by setting name.

        An image chart draws every overlay logged over its picture, and saves a row only for one it was told to treat
        differently. A table setting lists these rows in the editor anyway, to restyle: each is shown by its label,
        starting as the chart draws it (a blank row stands for every default), and cannot be renamed or removed, since
        removing it would change nothing. `choices` says what the experiment has. Defaults to none.
        """
        return FixedRows()

    @classmethod
    def viewer_state_type(cls) -> type[ViewerState[_Parameters]] | None:
        """
        The model of what a viewer changes in this chart without editing it: which layers are shown, a series hidden.

        Such state is the viewer's own and instant, kept in the browser (`RenderContext.viewer_state_key`), and never
        changes the saved chart. "Save as default" on the chart's card makes it the chart's own settings
        (`ViewerState.applied_to`). `None` (the default) means the chart keeps none.
        """
        return None


class ParameterField(BaseModel, frozen=True, extra="forbid"):
    """A field describing a chart parameter."""

    name: str
    type: ParameterFieldType
    required: bool
    default: bool | int | float | str | list[str] | dict[str, typing.Any] | None = None
    column_kind: ColumnKind | None = None
    choices: tuple[str, ...] | None = None
    """Fixed set of allowed values for a `Literal[...]`-typed field, rendered as a dropdown."""
    columns: tuple[ParameterField, ...] | None = None
    """For a `TABLE` field, a descriptor of each field of its row model: the cells of a row after its key."""
    minimum: float | None = None
    """The least a number field may be (its `ge`), if the settings model says."""
    maximum: float | None = None
    """The most a number field may be (its `le`), if the settings model says."""
    description: str | None = None
    """Help text shown alongside the field's widget: the field's attribute docstring on the settings model."""
    section: SettingsSection = DATA_SECTION
    """The group of settings the editor shows the field in."""
    title: str | None = None
    """What the editor calls the field, if the settings model says (`Field(title=...)`); else its name as a sentence."""

    @property
    def label(self) -> str:
        """What the editor calls the field: its `title`, or its name as a sentence (`tag_precision` is "Tag precision")."""
        return self.title or self.name.replace("_", " ").capitalize()


class UnknownChartTypeError(KeyError):
    """
    No chart plugin registered `chart_type_name`.

    A saved view names a chart type this deployment doesn't have installed (a plugin that was
    removed, or never installed on this server at all). Subclasses `KeyError` so existing
    `except KeyError` handling still catches it; callers that want to react to this specifically
    (as opposed to a bare lookup miss elsewhere) can catch it by name instead.
    """

    def __init__(self, chart_type_name: str) -> None:
        self.chart_type_name = chart_type_name
        super().__init__(
            f"No chart plugin registered for chart type {chart_type_name!r} -- it may need to be installed."
        )


def _fixed_choices(annotation: object) -> tuple[str, ...] | None:
    """The values a `Literal["number", "category"]` or a `StrEnum` allows, else `None`."""
    if typing.get_origin(annotation) is typing.Literal:
        return tuple(str(choice) for choice in typing.get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        enum_type: type[StrEnum] = annotation
        return tuple(str(member) for member in enum_type)
    return None


_SCALAR_WIDGETS: typing.Final = {
    bool: ParameterFieldType.BOOL,
    float: ParameterFieldType.FLOAT,
    int: ParameterFieldType.INT,
    str: ParameterFieldType.STR,
}


def _widget_for(annotation: object) -> tuple[ParameterFieldType, tuple[str, ...] | None]:
    """The widget a settings field is edited with, and its fixed choices (a dropdown) if it has any."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        # Unwrap `X | None` (an optional field) down to the underlying `X` — the widget
        # type is the same either way; only `required` (from `field.is_required()`) differs.
        non_none_args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none_args) == 1:
            annotation = non_none_args[0]
    if annotation in _SCALAR_WIDGETS:
        return _SCALAR_WIDGETS[annotation], None
    match annotation:
        case _ if typing.get_origin(annotation) is list and typing.get_args(annotation) == (str,):
            return ParameterFieldType.LIST_STR, None
        case _ if typing.get_origin(annotation) is dict:
            return ParameterFieldType.TABLE, None
        case _ if (choices := _fixed_choices(annotation)) is not None:
            # A fixed set of string choices — rendered as a dropdown rather than a free-text field.
            return ParameterFieldType.STR, choices
        case _:
            msg = f"{annotation} unsupported"
            raise TypeError(msg)


class ChartTypeRegistry:
    """Registry of all chart types."""

    _all_charts: typing.ClassVar[dict[str, type[ChartType[typing.Any, typing.Any, typing.Any]]]] = {}

    @classmethod
    def register(
        cls,
        chart_type: type[ChartType[typing.Any, typing.Any, typing.Any]],
        *,
        allow_override: bool = False,
    ) -> None:
        """Register a chart type."""
        if not allow_override and chart_type.name in cls._all_charts:
            msg = f"Duplicated chart type {chart_type.name=}"
            raise AttributeError(msg)
        cls._all_charts[chart_type.name] = chart_type

    @classmethod
    def get_chart_type(cls, chart_type_name: str) -> type[ChartType[typing.Any, typing.Any, typing.Any]]:
        """Fetch a registered chart type by name. Raises `UnknownChartTypeError` if none is registered."""
        if chart_type_name not in cls._all_charts:
            raise UnknownChartTypeError(chart_type_name)
        return cls._all_charts[chart_type_name]

    @classmethod
    def get_registered_chart_types(cls) -> dict[str, dict[str, ParameterField]]:
        """Expose chart metadata for simple editors and plugins."""
        return {
            name: cls._describe_parameter_fields(
                cls.get_chart_type(name).parameter_type(),
                cls.get_chart_type(name).field_column_kinds(),
            )
            for name in sorted(cls._all_charts)
        }

    @staticmethod
    def _describe_parameter_fields(
        parameter_type: type[BaseModel],
        field_column_kinds: dict[str, ColumnKind],
    ) -> dict[str, ParameterField]:
        """Describe parameter fields for generic UI generation."""
        field_descriptors: dict[str, ParameterField] = {}
        sections: dict[str, SettingsSection] = {}
        for field_name, field in parameter_type.model_fields.items():
            section = next((m for m in field.metadata if isinstance(m, SettingsSection)), DATA_SECTION)
            if sections.setdefault(section.title, section) != section:
                msg = (
                    f"{parameter_type.__name__} has two different settings sections titled {section.title!r}"
                )
                raise ValueError(msg)
            field_type, choices = _widget_for(field.annotation)
            if field_type == ParameterFieldType.LIST_STR and NamePatterns() in field.metadata:
                field_type = ParameterFieldType.LIST_PATTERN
            columns = (
                tuple(
                    ChartTypeRegistry._describe_parameter_fields(
                        typing.get_args(field.annotation)[1], {}
                    ).values()
                )
                if field_type == ParameterFieldType.TABLE
                else None
            )
            field_descriptors[field_name] = ParameterField(
                name=field_name,
                type=field_type,
                required=field.is_required(),
                default=field.get_default(call_default_factory=True) if not field.is_required() else None,
                column_kind=field_column_kinds.get(field_name),
                choices=choices,
                columns=columns,
                minimum=next((typing.cast("float", m.ge) for m in field.metadata if isinstance(m, Ge)), None),
                maximum=next((typing.cast("float", m.le) for m in field.metadata if isinstance(m, Le)), None),
                description=field.description,
                section=section,
                title=field.title,
            )
        return field_descriptors

    @classmethod
    def _typed_chart(
        cls, chart: ChartInstance[_Dataframe, _Chart]
    ) -> tuple[type[ChartType[typing.Any, typing.Any, typing.Any]], typing.Any]:
        """The registered chart type for `chart`, plus its parameters validated against that type's own model."""
        chart_type = cls.get_chart_type(chart.chart_type)
        return chart_type, chart_type.parameter_type().model_validate(chart.parameters)

    @classmethod
    def current_parameters(cls, chart: ChartInstance[_Dataframe, _Chart]) -> dict[str, object]:
        """
        `chart`'s settings as its chart type reads them now: what a form for editing it starts from.

        A chart saved before a setting was renamed or reshaped still loads (its type translates it), so
        a form built from the saved dict alone would show the new settings' defaults over it. A chart
        that no longer validates, or whose type is not installed, is edited as it was saved.
        """
        try:
            return cls._typed_chart(chart)[1].model_dump(mode="json")
        except (ValidationError, UnknownChartTypeError):
            return chart.parameters

    @classmethod
    def fixed_table_rows(cls, chart: ChartInstance[_Dataframe, _Chart], choices: ColumnChoices) -> FixedRows:
        """The rows `chart` draws without saving them (`ChartType.fixed_table_rows`); none if it cannot be read."""
        try:
            chart_type, parameters = cls._typed_chart(chart)
        except (ValidationError, UnknownChartTypeError):
            return {}
        return chart_type.fixed_table_rows(parameters, choices)

    @classmethod
    def render(
        cls, chart: ChartInstance[_Dataframe, _Chart], dataframe: object, context: RenderContext
    ) -> _Chart:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.render_in(parameters, dataframe, context)

    @classmethod
    def hint_required_columns(cls, chart: ChartInstance[_Dataframe, _Chart]) -> set[str] | None:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.hint_required_columns(parameters)

    @classmethod
    def hint_required_artifact_keys(cls, chart: ChartInstance[_Dataframe, _Chart]) -> set[str] | None:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.hint_required_artifact_keys(parameters)

    @classmethod
    def hint_required_artifact_key_prefixes(cls, chart: ChartInstance[_Dataframe, _Chart]) -> set[str]:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.hint_required_artifact_key_prefixes(parameters)

    @classmethod
    def hint_required_histogram_keys(cls, chart: ChartInstance[_Dataframe, _Chart]) -> set[str]:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.hint_required_histogram_keys(parameters)

    @classmethod
    def hint_required_hparams(cls, chart: ChartInstance[_Dataframe, _Chart]) -> set[str] | None:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.hint_required_hparams(parameters)

    @classmethod
    def natural_width(cls, chart: ChartInstance[_Dataframe, _Chart], context: RenderContext) -> int:
        chart_type, parameters = cls._typed_chart(chart)
        return chart_type.natural_width_in(parameters, context)

    @classmethod
    def keeps_viewer_state(cls, chart: ChartInstance[_Dataframe, _Chart]) -> bool:
        """Whether a viewer can change `chart` without editing it (see `ChartType.viewer_state_type`)."""
        try:
            return cls.get_chart_type(chart.chart_type).viewer_state_type() is not None
        except UnknownChartTypeError:
            return False

    @classmethod
    def saved_with_viewer_state(
        cls, chart: ChartInstance[_Dataframe, _Chart], state: object
    ) -> ChartInstance[_Dataframe, _Chart]:
        """
        `chart` with a viewer's `state` (as the browser kept it) made its own settings, under the same id.

        Validated by the chart type's own state model, so whatever the browser sent is checked before it is saved.
        """
        chart_type, parameters = cls._typed_chart(chart)
        state_type = chart_type.viewer_state_type()
        if state_type is None:
            msg = f"{chart.chart_type} keeps no viewer state"
            raise ValueError(msg)
        saved: BaseModel = state_type.model_validate(state).applied_to(parameters)
        return chart.model_copy(update={"parameters": saved.model_dump(mode="json")})


class ChartInstance(BaseModel, typing.Generic[_Dataframe, _Chart], frozen=True, extra="forbid"):
    """A chart for a set of metrics."""

    id: str = ""
    """
    A stable handle for this chart (what a `?chart=` deep link points at), unlike its position.

    Never needs to be passed: when absent -- a new chart, or one saved before ids existed -- it's
    derived from the chart's type and parameters, so it's stable across loads without a write. Once
    saved it's kept as-is, so editing a chart's parameters later doesn't change it. Two charts with
    identical type and parameters on one page share an id; a link to either opens the first.
    """

    chart_type: str
    """The type of chart."""

    parameters: dict[str, object] = {}
    """Parameters for the specific chart."""

    @model_validator(mode="before")
    @classmethod
    def _default_id_from_content(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        fields = typing.cast("dict[str, object]", data)
        if fields.get("id"):
            return fields
        content = json.dumps(
            [fields.get("chart_type"), fields.get("parameters", {})], sort_keys=True, default=str
        )
        return {**fields, "id": hashlib.sha256(content.encode()).hexdigest()[:_CHART_ID_LENGTH]}

    def render(self, dataframe: _Dataframe, context: RenderContext = DEFAULT_RENDER_CONTEXT) -> _Chart:
        """Render the chart instance."""
        return ChartTypeRegistry.render(self, dataframe, context)

    def hint_required_columns(self) -> set[str] | None:
        """
        Hint the required columns for this chart to render.

        Falls back to `None` ("fetch everything") if `parameters` no longer validates against this
        chart type's settings model (e.g. stale data from before a schema change) or if no chart
        plugin is registered for `chart_type` at all (e.g. one that was uninstalled) -- rather than
        raising and taking the rest of the panel's data-fetching down with it. `render()` still
        raises for the same cases, so the broken chart itself still surfaces as its own inline error.
        """
        try:
            return ChartTypeRegistry.hint_required_columns(self)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, fetching every column "
                "as a fallback",
                chart_type=self.chart_type,
            )
            return None

    def hint_required_artifact_keys(self) -> set[str] | None:
        try:
            return ChartTypeRegistry.hint_required_artifact_keys(self)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, no artifact-key hint available",
                chart_type=self.chart_type,
            )
            return None

    def hint_required_artifact_key_prefixes(self) -> set[str]:
        try:
            return ChartTypeRegistry.hint_required_artifact_key_prefixes(self)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, no artifact-key prefix hint available",
                chart_type=self.chart_type,
            )
            return set()

    def hint_required_histogram_keys(self) -> set[str]:
        try:
            return ChartTypeRegistry.hint_required_histogram_keys(self)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, no histogram-key hint available",
                chart_type=self.chart_type,
            )
            return set()

    def hint_required_hparams(self) -> set[str] | None:
        """Hint the required hyperparameter keys for this chart to render. See `hint_required_columns`."""
        try:
            return ChartTypeRegistry.hint_required_hparams(self)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, fetching every hparam "
                "as a fallback",
                chart_type=self.chart_type,
            )
            return None

    def natural_width(self, context: RenderContext = DEFAULT_RENDER_CONTEXT) -> int:
        """Preferred render width in px for this chart instance. See `hint_required_columns`."""
        try:
            return ChartTypeRegistry.natural_width(self, context)
        except (ValidationError, UnknownChartTypeError):
            _log.warning(
                "chart has invalid parameters or an unregistered chart type, using fallback width",
                chart_type=self.chart_type,
            )
            return _FALLBACK_NATURAL_WIDTH


class PanelInstance(BaseModel, typing.Generic[_Dataframe, _Chart], frozen=True, extra="forbid"):
    """A panel containing one or more charts."""

    name: str = ""
    """Human-readable name for the panel."""

    tab: str = ""
    """Which tab this panel is grouped under; empty means the default/ungrouped tab."""

    charts: list[ChartInstance[_Dataframe, _Chart]] = []
    """Charts belonging to this panel."""

    sync: bool = True
    """Whether synced-capable charts in this panel move together: line charts share a hover/tooltip crosshair, image series their step slider."""

    layout: typing.Literal["packed", "grid"] = "packed"
    """`"packed"` sizes each chart to its own natural width and wraps them left-to-right;
    `"grid"` forces every chart onto an equal-width column instead."""

    grid_columns: GridColumns = 3
    """How many equal-width columns the `"grid"` layout has; unused by `"packed"`."""

    def render(
        self, dataframes: _Dataframe, page_size: int = DEFAULT_RENDER_CONTEXT.page_size
    ) -> list[_Chart]:
        """Render a panel."""
        context = RenderContext(columns=self.grid_columns, page_size=page_size)
        return [c.render(dataframes, context) for c in self.charts]

    def hint_required_columns(self) -> set[str] | None:
        """
        Hint the required columns for the panel.

        ## Performance!
        If all charts in a panel hint their required columns:
        A page should only fetch the required columns for the panel.
        """
        columns: set[str] = set()
        for chart in self.charts:
            hint = chart.hint_required_columns()
            if hint is None:
                return None
            columns |= hint
        return columns

    def hint_required_artifact_keys(self) -> set[str | None]:
        return set(itertools.chain(*[c.hint_required_artifact_keys() or set() for c in self.charts]))

    def hint_required_artifact_key_prefixes(self) -> set[str]:
        return set(
            itertools.chain.from_iterable(c.hint_required_artifact_key_prefixes() for c in self.charts)
        )

    def hint_required_histogram_keys(self) -> set[str]:
        return set(itertools.chain.from_iterable(c.hint_required_histogram_keys() for c in self.charts))

    def hint_required_hparams(self) -> set[str] | None:
        """Hint the required hyperparameter keys for the panel; `None` means "fetch every one"."""
        hparams: set[str] = set()
        for chart in self.charts:
            hint = chart.hint_required_hparams()
            if hint is None:
                return None
            hparams |= hint
        return hparams


class NewPage(BaseModel, typing.Generic[_Dataframe, _Chart], frozen=True, extra="forbid"):
    """A page view model."""

    run_id: int | None = None
    """Run ID for the experiment, could be none."""

    experiment_id: int | None = None
    """The experiment id associated with this page, could be none."""

    project_id: int | None = None
    """Project id for this page, could be none."""

    owner_id: int | None = None
    """
    `None` for the shared page everyone sees; a user's id for that user's own named view of it.

    A view is an independent copy of the page's panels and settings: editing it never touches the
    shared page (or anyone else's view), and vice versa.
    """

    name: str = ""
    """A view's name, as listed in the view picker. Empty for the shared page."""

    shared: bool = False
    """
    Whether a named view (`owner_id` set) is visible to every project viewer, not just its owner.

    Meaningless for the shared page itself (`owner_id` is `None`), which every project viewer
    already sees. A `False` view is private: only its owner can open it at all, by id/URL or
    otherwise (see `AuthorizingDataStore.get_view`) -- `shared` is what makes a view reachable, and
    listed in everyone else's own view picker, the same as one of their own views.
    """

    panels: list[PanelInstance[_Dataframe, _Chart]] = []
    """The set of panel instances on a page."""

    page_settings: dict[str, int | float | bool | str | list[str] | list[int] | None] = {}
    """Page settings."""

    @field_validator("panels", "page_settings", mode="before")
    @classmethod
    def deserialize_json(cls, raw_value: dict[str, object] | str) -> dict[str, object]:
        if isinstance(raw_value, str):
            return json.loads(raw_value)
        return raw_value


class Page(NewPage[_Dataframe, _Chart], typing.Generic[_Dataframe, _Chart], frozen=True, extra="forbid"):
    """A page stored in sql."""

    id: int
    """The id the page is stored under."""


class ViewSummary(BaseModel, frozen=True, extra="forbid"):
    """Just enough of a saved view to list it in a picker."""

    id: int
    name: str
