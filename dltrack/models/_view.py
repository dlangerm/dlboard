"""Data models for rendering an experiment view."""

from __future__ import annotations

import itertools
import typing
from abc import ABC, abstractmethod

from pydantic import BaseModel

if typing.TYPE_CHECKING:
    from collections.abc import Iterable

    from dltrack.models._data_store import DataStore


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
        return None

    @classmethod
    def register(cls, *, allow_override: bool = False) -> None:
        ChartTypeRegistry.register(cls, allow_override=allow_override)


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
    def render[T, C](cls, chart: ChartInstance[T, C], dataframe: object) -> C:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.render(chart_type.parameter_type().model_validate(chart.parameters), dataframe)

    @classmethod
    def hint_required_columns[T, C](cls, chart: ChartInstance[T, C]) -> set[str] | None:
        chart_type = cls._all_charts[chart.chart_type]
        return chart_type.hint_required_columns(chart_type.parameter_type().model_validate(chart.parameters))


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


class PanelInstance[D, C](BaseModel, frozen=True, extra="forbid"):
    """A panel containing one or more charts."""

    id: str
    """Unique id to identify this panel."""

    charts: list[ChartInstance[D, C]] = []
    """Charts belonging to this panel."""

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
        hints = set(itertools.chain(*[c.hint_required_columns() or [None] for c in self.charts]))
        if None in hints:
            return None
        return hints  # pyright: ignore[reportReturnType]


class Page[D, P, C](BaseModel, frozen=True, extra="forbid"):
    """A page view model."""

    panels: list[PanelInstance[D, C]] = []
    """The set of panel instances on a page."""

    page_settings: dict[str, int | float | bool | str | list[str] | None] = {}
    """Page settings."""

    @abstractmethod
    def retrieve_dataframes(self, store: DataStore[...], experiment_id: int) -> Iterable[D]:
        """Get the dataframes for a page."""

    @abstractmethod
    def render(self, data_store: DataStore[...], experiment_id: int) -> P:
        """Render the page."""


class ExperimentView(BaseModel, frozen=True, extra="forbid"):
    """A view of an experiment."""

    id: int
    """The ID of the experiment."""


class ProjectView(BaseModel, frozen=True, extra="forbid"):
    """A view of a project."""

    id: int
    """The ID of the project."""


class RunView(BaseModel, frozen=True, extra="forbid"):
    """A view of a run."""

    id: int
    """The ID of the run."""
