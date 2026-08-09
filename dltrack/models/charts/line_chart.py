"""A line chart."""

from __future__ import annotations

import contextlib
import typing
from hashlib import md5

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel

from dltrack.models._view import ChartType

colors = [
    "gray",
    "red",
    "pink",
    "grape",
    "violet",
    "indigo",
    "blue",
    "cyan",
    "teal",
    "green",
    "lime",
    "yellow",
    "orange",
]


def _hash_color(run_id: int, temperature: int = 5) -> str:
    v = int(md5(str(run_id).encode(), usedforsecurity=False).hexdigest(), base=16) % len(colors)
    return colors[v] + f".{temperature % 10}"


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

        for c in dataframe.columns:
            if str(dataframe[c].dtype) in ("object", "str"):
                with contextlib.suppress(ValueError):
                    dataframe[c] = pd.to_datetime(dataframe[c], utc=True, format="ISO8601")

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
                    "color": _hash_color(int(col)),
                }
                for col in df.columns
                if col != parameters.x_axis
            ],  # pyright: ignore[reportArgumentType]
            xAxisLabel=f"{parameters.x_axis}",
            yAxisLabel=f"{parameters.column}",
            withLegend=True,
            withXAxis=True,
            withYAxis=True,
            withDots=False,
            tickLine="xy",
            lineChartProps={"syncId": parameters.x_axis},
        )

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: LineChartSettings) -> set[str] | None:
        return {
            parameters.column,
            parameters.x_axis,
        }


LineChart.register()
