"""A line chart."""

from __future__ import annotations

import typing

import dash_mantine_components as dmc  # pyright: ignore[reportMissingTypeStubs]
import pandas as pd
from pydantic import BaseModel

from dltrack.models._view import ChartType


class LineChartSettings(BaseModel, frozen=True, extra="forbid"):
    """Line chart settings."""

    column: str
    x_axis: str
    height: int = 300


class LineChart(ChartType[LineChartSettings, pd.DataFrame, dmc.LineChart], frozen=True, extra="forbid"):
    """a line chart."""

    name: typing.ClassVar[str] = "line"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[LineChartSettings]:
        return LineChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: LineChartSettings, dataframe: pd.DataFrame) -> dmc.LineChart:
        x_col = parameters.x_axis
        axis_cols = ["run_id", "step"]
        if x_col != "step":
            axis_cols.append(x_col)
        axis_df = dataframe.loc[dataframe[x_col].notna(), axis_cols]
        value_df = dataframe.loc[dataframe[parameters.column].notna(), ["run_id", "step", parameters.column]]

        df = axis_df.merge(value_df, on=["run_id", "step"], how="inner")
        df = df.groupby([parameters.x_axis, "run_id"], as_index=False)[parameters.column].mean()
        df = df.pivot(index=parameters.x_axis, columns="run_id", values=parameters.column).reset_index()
        return dmc.LineChart(
            h=parameters.height,
            data=df.to_dict(orient="records"),
            dataKey=str(parameters.x_axis),
            series=[
                {
                    "name": str(col),
                    "label": f"Run {col}",
                    "color": f"indigo.{int(col) % 6}",
                    "curveType": "natural",
                }
                for col in df.columns
                if col != parameters.x_axis
            ],  # pyright: ignore[reportArgumentType]
            withDots=False,
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: LineChartSettings) -> set[str] | None:
        return {
            parameters.column,
            parameters.x_axis,
        }


LineChart.register()
