"""Data models for rendering an experiment view."""

from __future__ import annotations

import itertools
import json
import typing
from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, field_validator

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from dltrack.models._data_store import DataStore


class ColumnKind(StrEnum):
    """Registry of dataframe column kinds a chart parameter field can be populated from."""

    METRIC = "metric"
    ARTIFACT = "artifact"


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
    type: typing.Literal["bool", "int", "float", "str"]
    required: bool
    default: bool | int | float | str | None = None
    column_kind: ColumnKind | None = None


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
            if annotation is bool:
                field_type = "bool"
            elif annotation is float:
                field_type = "float"
            elif annotation is int:
                field_type = "int"
            elif annotation is str:
                field_type = "str"
            else:
                msg = f"{annotation} unsupported"
                raise TypeError(msg)

            field_descriptors[field_name] = ParameterField(
                name=field_name,
                type=field_type,
                required=field.is_required(),
                default=field.default if not field.is_required() else None,
                column_kind=field_column_kinds.get(field_name),
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


class ChartInstance[D, C](BaseModel, frozen=True, extra="forbid"):
    """A chart for a set of metrics."""

    chart_type: str
    """The type of chart."""

    parameters: dict[str, object] = {}
    """Parameters for the specific chart."""

    def render(self, dataframe: D) -> C:
        """Render the chart instance."""
        return ChartTypeRegistry.render(self, dataframe)

    def hint_required_columns(self) -> set[str] | None:
        """Hint the required columns for this chart to render."""
        return ChartTypeRegistry.hint_required_columns(self)

    def hint_required_artifact_keys(self) -> set[str] | None:
        return ChartTypeRegistry.hint_required_artifact_keys(self)


class PanelInstance[D, C](BaseModel, frozen=True, extra="forbid"):
    """A panel containing one or more charts."""

    name: str = ""
    """Human-readable name for the panel."""

    charts: list[ChartInstance[D, C]] = []
    """Charts belonging to this panel."""

    @property
    def display_name(self) -> str:
        """Return a display-friendly panel name."""
        return self.name

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


class NewPage[D, C](BaseModel, frozen=True, extra="forbid"):
    """A page view model."""

    run_id: int | None = None
    """Run ID for the experiment, could be none."""

    experiment_id: int | None = None
    """The experiment id associated with this page, could be none."""

    project_id: int | None = None
    """Project id for this page, could be none."""

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
    def retrieve_dataframes(self, store: DataStore[...], experiment_id: int) -> Iterable[D]:
        """Get the dataframes for a page."""

    @abstractmethod
    def render(self, data_store: DataStore[...], experiment_id: int) -> P:
        """Render the page."""

    @classmethod
    def sql_schema(cls) -> dict[str, str]:
        """Return the sql schema for this type."""
        return {
            "id": "PRIMARY KEY AUTOINCREMENT",
            "panels": "TEXT",
            "page_settings": "TEXT",
        }
