"""
What produced each run shown -- its commit, how it was started, the machine and the packages -- a fact a row, a run a column.

Read from each run's `RunContext` (`DataRequest.run_context`), recorded by the client as the run started, and its
uncommitted changes (`RUN_CONTEXT_DIFF_KEY`), linked for download under the table when a run logged them.
"""

from __future__ import annotations

import typing
from typing import Any, Final

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import (
    DISPLAY_SECTION,
    RUN_CONTEXT_COLUMN,
    RUN_CONTEXT_DIFF_KEY,
    RUN_NAME_COLUMN,
    Artifact,
    ChartType,
    ColumnKind,
    ContextSection,
    DataRequest,
    MetricColumn,
    RunContext,
    ValidJsonTypes,
    artifact_column,
)
from dlboard.serve import series_swatch_class
from dlboard.serve._grid import TEXT, column_def, grid

if typing.TYPE_CHECKING:
    import dash
    import dash_ag_grid as dag

_SECTION_LABELS: Final = {
    ContextSection.GIT: "Git",
    ContextSection.COMMAND: "Command",
    ContextSection.MACHINE: "Machine",
    ContextSection.PACKAGES: "Packages",
}


class RunContextSettings(BaseModel, frozen=True, extra="forbid", use_attribute_docstrings=True):
    """Run context settings: which facts the table lists."""

    git: bool = True
    """List the commit, the branch, the `origin` remote and whether there were changes not committed."""
    command: bool = True
    """List the command line, the working directory and the interpreter."""
    machine: bool = True
    """List the Python version, the platform, the host, the CUDA version and the GPUs."""
    packages: bool = True
    """List each installed distribution's version."""
    only_differences: bool = False
    """List only the facts whose value is not the same in every run shown."""
    page_size: typing.Annotated[int, DISPLAY_SECTION] = Field(default=20, ge=1)
    """How many facts each page of the table shows."""
    width: typing.Annotated[int, DISPLAY_SECTION] = Field(default=760, ge=320)
    """Overall chart width in px."""

    def shows(self, section: ContextSection) -> bool:
        """Whether the table lists `section`'s facts."""
        match section:
            case ContextSection.GIT:
                return self.git
            case ContextSection.COMMAND:
                return self.command
            case ContextSection.MACHINE:
                return self.machine
            case ContextSection.PACKAGES:
                return self.packages


class _Run(typing.NamedTuple):
    """One run's column: its id, what it is called, its context, and its uncommitted changes' download, if it logged them."""

    id: int
    name: str
    context: RunContext
    diff_url: str | None


def _runs(dataframe: pd.DataFrame) -> list[_Run]:
    """The runs of the frame that recorded a context, in the order it lists them."""
    if dataframe.empty or RUN_CONTEXT_COLUMN not in dataframe.columns:
        return []
    diff_column = artifact_column(RUN_CONTEXT_DIFF_KEY)
    diffs = (
        {
            int(run): str(url)
            for run, url in dataframe.loc[
                dataframe[diff_column].notna(), [MetricColumn.RUN_ID, diff_column]
            ].itertuples(index=False)
        }
        if diff_column in dataframe.columns
        else dict[int, str]()
    )
    recorded = dataframe[dataframe[RUN_CONTEXT_COLUMN].notna()].drop_duplicates(MetricColumn.RUN_ID)
    runs: list[_Run] = []
    for row in recorded.to_dict("records"):
        run_id = int(row[MetricColumn.RUN_ID])
        name = row.get(RUN_NAME_COLUMN)
        runs.append(
            _Run(
                run_id,
                name if isinstance(name, str) else f"Run {run_id}",
                RunContext.model_validate(row[RUN_CONTEXT_COLUMN]),
                diffs.get(run_id),
            )
        )
    return runs


def _field(run_id: int) -> str:
    return f"run_{run_id}"


def _shown(value: ValidJsonTypes) -> str | None:
    """A fact as the table shows it: a flag as yes or no, anything else as recorded; `None` where it wasn't."""
    match value:
        case None:
            return None
        case bool():
            return "yes" if value else "no"
        case _:
            return str(value)


def context_rows(parameters: RunContextSettings, runs: list[_Run]) -> list[dict[str, Any]]:
    """
    A row per fact any of `runs` recorded, of the sections `parameters` shows, in their order, with each run's value.

    With `only_differences`, only the facts whose value is not the same in every run (one a run lacks included).
    """
    by_run = [(run, run.context.sections()) for run in runs]
    rows: list[dict[str, Any]] = []
    for section in ContextSection:
        if not parameters.shows(section):
            continue
        # Each fact once, in the order the runs list them: the packages sorted, the rest as `RunContext.sections` has them.
        for fact in dict.fromkeys(fact for _, sections in by_run for fact in sections[section]):
            values = {_field(run.id): _shown(sections[section].get(fact)) for run, sections in by_run}
            if parameters.only_differences and len(set(values.values())) < 2:  # noqa: PLR2004
                continue
            label = fact.removeprefix(f"{section}/")
            rows.append({"section": _SECTION_LABELS[section], "fact": label, **values})
    return rows


def _grid(parameters: RunContextSettings, runs: list[_Run], rows: list[dict[str, Any]]) -> dag.AgGrid:
    return grid(
        columnDefs=[
            column_def("section", TEXT, headerName="Section", width=110, pinned="left"),
            column_def("fact", TEXT, headerName="Fact", width=170, pinned="left", floatingFilter=True),
            *(
                column_def(
                    _field(run.id),
                    TEXT,
                    headerName=run.name,
                    headerClass=series_swatch_class(run.id),
                    flex=1,
                    minWidth=180,
                    tooltipField=_field(run.id),
                )
                for run in runs
            ),
        ],
        rowData=rows,
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": parameters.page_size,
            "paginationPageSizeSelector": sorted({parameters.page_size, 20, 50, 100}),
            "domLayout": "autoHeight",
            "tooltipShowDelay": 400,
        },
        class_name="dl-run-context",
    )


def _diff_links(runs: list[_Run]) -> list[dmc.Group]:
    """Each run's uncommitted changes, for download, under the table: none when no run shown logged them."""
    logged = [run for run in runs if run.diff_url is not None]
    if not logged:
        return []
    return [
        dmc.Group(
            [
                dmc.Text("Uncommitted changes:", size="xs", c="dimmed"),
                *(
                    dmc.Anchor(
                        f"{run.name} (git.diff)" if len(runs) > 1 else "git.diff",
                        href=run.diff_url,
                        size="xs",
                    )
                    for run in logged
                ),
            ],
            gap="xs",
        )
    ]


class RunContextChart(ChartType[RunContextSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """What produced each run shown: its commit, command, machine and packages, side by side, and its uncommitted changes."""

    name: typing.ClassVar[str] = "run_context"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[RunContextSettings]:
        return RunContextSettings

    @typing.override
    @classmethod
    def can_display_artifact(cls, artifact: Artifact) -> bool:
        """A run's uncommitted changes, which this lists with the rest of what produced the run."""
        return artifact.key == RUN_CONTEXT_DIFF_KEY

    @typing.override
    @classmethod
    def can_display_run_context(cls) -> bool:
        return True

    @typing.override
    @classmethod
    def render(cls, parameters: RunContextSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        runs = _runs(dataframe)
        if not runs:
            return dmc.Stack([dmc.Text("No context was recorded for the runs shown", c="dimmed")])
        rows = context_rows(parameters, runs)
        table = (
            _grid(parameters, runs, rows)
            if rows
            else dmc.Text(
                "Every run shown has the same context" if parameters.only_differences else "No facts to list",
                c="dimmed",
                size="sm",
            )
        )
        # The chart's cell sets its width (`natural_width` where the panel packs its charts): it fills it.
        return dmc.Stack([table, *_diff_links(runs)], gap="xs")

    @typing.override
    @classmethod
    def data_request(cls, parameters: RunContextSettings) -> DataRequest:
        return DataRequest(run_context=True, artifacts=frozenset({RUN_CONTEXT_DIFF_KEY}))

    @typing.override
    @classmethod
    def natural_width(cls, parameters: RunContextSettings) -> int:
        return parameters.width

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {}


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type."""
    del app
    RunContextChart.register(allow_override=True)
