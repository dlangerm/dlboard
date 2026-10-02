"""Data models for rendering an experiment view."""

from __future__ import annotations

import hashlib
import itertools
import json
import types
import typing
from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ValidationError, field_validator, model_validator
from structlog.stdlib import get_logger

if typing.TYPE_CHECKING:
    from dltrack.models._data_store import DataStore

_log = get_logger(__name__)

_CHART_ID_LENGTH = 10

_FALLBACK_NATURAL_WIDTH: typing.Final = 400
"""Width used for a chart whose persisted `parameters` no longer validate (e.g. after a schema
change), so a single broken chart can't crash the whole panel's layout -- `render()` still raises
for the same case, surfacing as that one chart's own inline error instead."""


class ColumnKind(StrEnum):
    """Registry of dataframe column kinds a chart parameter field can be populated from."""

    METRIC = "metric"
    ARTIFACT = "artifact"
    HPARAM = "hparam"
    GROUPING = "grouping"
    """Synthetic kind for fields that group/split by any metric or hparam column, plus `run_id` --
    see `group_columns_by_kind`. No dataframe column is ever tagged with this kind directly."""


RUN_NAME_COLUMN: typing.Final = "run_name"
"""
The column a run's display name is carried under, when a chart's dataframe has one.

Not a real metric/hparam/artifact column -- never fetched from the store directly, and not part
of `MetricColumn`'s "fixed columns of a `MetricFrame`" -- it's merged onto a panel's fetched
dataframe afterward (see `fetch_panel_dataframe`), so a chart can label a run by name instead of
its bare id without fetching run metadata itself. One shared symbol (rather than every chart type
repeating the string literal) so a plugin and the page code that feeds it stay in sync, and so a
chart that needs to *exclude* non-metric columns (e.g. `table_chart.py`'s "runs" mode) can name it
precisely rather than guessing at string prefixes.
"""


class ParameterFieldType(StrEnum):
    """Widget type a chart parameter field should be rendered as."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    LIST_STR = "list[str]"


class ChartType[P: BaseModel, D, C](ABC, BaseModel, frozen=True, extra="forbid"):
    """A type of chart."""

    name: typing.ClassVar[str]
    """The name of the chart type."""

    @classmethod
    @abstractmethod
    def parameter_type(cls) -> type[P]:
        """The parameter container type."""

    @classmethod
    @abstractmethod
    def render(cls, parameters: P, dataframe: D) -> C:
        """Render a chart given an instance."""

    @classmethod
    @abstractmethod
    def hint_required_columns(cls, parameters: P) -> set[str] | None:
        """Hint at the columns required for this chart."""

    @classmethod
    @abstractmethod
    def hint_required_artifact_keys(cls, parameters: P) -> set[str] | None:
        """Hint at the columns required for this chart."""

    @classmethod
    @abstractmethod
    def hint_required_hparams(cls, parameters: P) -> set[str] | None:
        """Hint at the hyperparameter keys required for this chart."""

    @classmethod
    @abstractmethod
    def natural_width(cls, parameters: P) -> int:
        """Preferred render width in px, derived from this chart's own settings (e.g. height + aspect ratio)."""

    @classmethod
    def register(cls, *, allow_override: bool = False) -> None:
        ChartTypeRegistry.register(cls, allow_override=allow_override)

    @classmethod
    @abstractmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        """
        Map string parameter field names to the column kind that populates them.

        Fields left unlisted default to ColumnKind.METRIC.
        """


class ParameterField(BaseModel, frozen=True, extra="forbid"):
    """A field describing a chart parameter."""

    name: str
    type: ParameterFieldType
    required: bool
    default: bool | int | float | str | list[str] | None = None
    column_kind: ColumnKind | None = None
    choices: tuple[str, ...] | None = None
    """Fixed set of allowed values for a `Literal[...]`-typed field, rendered as a dropdown."""
    description: str | None = None
    """Help text shown alongside the field's widget, sourced from the settings model's own
    `pydantic.Field(description=...)`."""


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
        """Fetch a registered chart type by name."""
        if chart_type_name not in cls._all_charts:
            msg = f"Unknown chart type {chart_type_name=}"
            raise KeyError(msg)
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
        field_column_kinds = field_column_kinds
        field_descriptors: dict[str, ParameterField] = {}
        for field_name, field in parameter_type.model_fields.items():
            annotation = field.annotation
            if typing.get_origin(annotation) in (typing.Union, types.UnionType):
                # Unwrap `X | None` (an optional field) down to the underlying `X` — the widget
                # type is the same either way; only `required` (from `field.is_required()`) differs.
                non_none_args = [a for a in typing.get_args(annotation) if a is not type(None)]
                if len(non_none_args) == 1:
                    annotation = non_none_args[0]
            choices: tuple[str, ...] | None = None
            match annotation:
                case _ if annotation is bool:
                    field_type = ParameterFieldType.BOOL
                case _ if annotation is float:
                    field_type = ParameterFieldType.FLOAT
                case _ if annotation is int:
                    field_type = ParameterFieldType.INT
                case _ if annotation is str:
                    field_type = ParameterFieldType.STR
                case _ if typing.get_origin(annotation) is list and typing.get_args(annotation) == (str,):
                    field_type = ParameterFieldType.LIST_STR
                case _ if typing.get_origin(annotation) is typing.Literal:
                    # A fixed set of string choices, e.g. `Literal["number", "category"]` — rendered
                    # as a dropdown rather than a free-text field.
                    field_type = ParameterFieldType.STR
                    choices = typing.get_args(annotation)
                case _:
                    msg = f"{annotation} unsupported"
                    raise TypeError(msg)

            field_descriptors[field_name] = ParameterField(
                name=field_name,
                type=field_type,
                required=field.is_required(),
                default=field.default if not field.is_required() else None,
                column_kind=field_column_kinds.get(field_name),
                choices=choices,
                description=field.description,
            )
        return field_descriptors

    @classmethod
    def render[T, C](cls, chart: ChartInstance[T, C], dataframe: object) -> C:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.render(chart_type.parameter_type().model_validate(chart.parameters), dataframe)

    @classmethod
    def hint_required_columns[T, C](cls, chart: ChartInstance[T, C]) -> set[str] | None:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.hint_required_columns(chart_type.parameter_type().model_validate(chart.parameters))

    @classmethod
    def hint_required_artifact_keys[T, C](cls, chart: ChartInstance[T, C]) -> set[str] | None:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.hint_required_artifact_keys(
            chart_type.parameter_type().model_validate(chart.parameters)
        )

    @classmethod
    def hint_required_hparams[T, C](cls, chart: ChartInstance[T, C]) -> set[str] | None:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.hint_required_hparams(chart_type.parameter_type().model_validate(chart.parameters))

    @classmethod
    def natural_width[T, C](cls, chart: ChartInstance[T, C]) -> int:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.natural_width(chart_type.parameter_type().model_validate(chart.parameters))


class ChartInstance[D, C](BaseModel, frozen=True, extra="forbid"):
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

    def render(self, dataframe: D) -> C:
        """Render the chart instance."""
        return ChartTypeRegistry.render(self, dataframe)

    def hint_required_columns(self) -> set[str] | None:
        """
        Hint the required columns for this chart to render.

        Falls back to `None` ("fetch everything") if `parameters` no longer validates against this
        chart type's settings model -- e.g. stale data from before a schema change -- rather than
        raising and taking the rest of the panel down with it. `render()` still raises for the same
        case, so the broken chart itself still surfaces as its own inline error.
        """
        try:
            return ChartTypeRegistry.hint_required_columns(self)
        except ValidationError:
            _log.warning(
                "chart has invalid parameters, fetching every column as a fallback",
                chart_type=self.chart_type,
            )
            return None

    def hint_required_artifact_keys(self) -> set[str] | None:
        try:
            return ChartTypeRegistry.hint_required_artifact_keys(self)
        except ValidationError:
            _log.warning(
                "chart has invalid parameters, no artifact-key hint available", chart_type=self.chart_type
            )
            return None

    def hint_required_hparams(self) -> set[str] | None:
        """Hint the required hyperparameter keys for this chart to render. See `hint_required_columns`."""
        try:
            return ChartTypeRegistry.hint_required_hparams(self)
        except ValidationError:
            _log.warning(
                "chart has invalid parameters, fetching every hparam as a fallback",
                chart_type=self.chart_type,
            )
            return None

    def natural_width(self) -> int:
        """Preferred render width in px for this chart instance. See `hint_required_columns`."""
        try:
            return ChartTypeRegistry.natural_width(self)
        except ValidationError:
            _log.warning("chart has invalid parameters, using fallback width", chart_type=self.chart_type)
            return _FALLBACK_NATURAL_WIDTH


class PanelInstance[D, C](BaseModel, frozen=True, extra="forbid"):
    """A panel containing one or more charts."""

    name: str = ""
    """Human-readable name for the panel."""

    tab: str = ""
    """Which tab this panel is grouped under; empty means the default/ungrouped tab."""

    charts: list[ChartInstance[D, C]] = []
    """Charts belonging to this panel."""

    sync: bool = True
    """Whether synced-capable charts (e.g. line charts) in this panel share a hover/tooltip crosshair."""

    layout: typing.Literal["packed", "grid"] = "packed"
    """`"packed"` sizes each chart to its own natural width and wraps them left-to-right;
    `"grid"` forces every chart onto an equal-width column instead."""

    def render(self, dataframes: D) -> list[C]:
        """Render a panel."""
        return [c.render(dataframes) for c in self.charts]

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

    def hint_required_hparams(self) -> set[str] | None:
        """Hint the required hyperparameter keys for the panel; `None` means "fetch every one"."""
        hparams: set[str] = set()
        for chart in self.charts:
            hint = chart.hint_required_hparams()
            if hint is None:
                return None
            hparams |= hint
        return hparams


class NewPage[D, C](BaseModel, frozen=True, extra="forbid"):
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

    panels: list[PanelInstance[D, C]] = []
    """The set of panel instances on a page."""

    page_settings: dict[str, int | float | bool | str | list[str] | list[int] | None] = {}
    """Page settings."""

    @field_validator("panels", "page_settings", mode="before")
    @classmethod
    def deserialize_json(cls, raw_value: dict[str, object] | str) -> dict[str, object]:
        if isinstance(raw_value, str):
            return json.loads(raw_value)
        return raw_value


class Page[D, P, C](NewPage[D, C], frozen=True, extra="forbid"):
    """A page stored in sql."""

    id: int
    """Page ID to be rendered."""

    @abstractmethod
    def render(self, data_store: DataStore[...], experiment_id: int) -> P:
        """Render the page."""


class ViewSummary(BaseModel, frozen=True, extra="forbid"):
    """Just enough of a saved view to list it in a picker."""

    id: int
    name: str
