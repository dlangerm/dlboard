"""A file list with download links: every file logged under one artifact key, or a whole family of keys."""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any, Final, cast

import dash_ag_grid as dag
import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field, model_validator

from dlboard.models import FILE_KIND_TAG, RUN_NAME_COLUMN, Artifact, ChartType, ColumnKind, MetricColumn
from dlboard.plugins.charts._table_style import (
    ARTIFACT_COLUMN_PREFIX,
    NUMERIC,
    TEXT,
    artifact_column,
    column_def,
    themed_grid_kwargs,
)
from dlboard.serve import AssetKind, serve_asset

if typing.TYPE_CHECKING:
    import dash

_CSS_PATH: Final = Path(__file__).with_name("file_list.css")

_TAG_KEY: Final = "tag"
"""The tag the Lightning logger records what a checkpoint is under: `latest`, `best` or `best_k`."""
_SCORE_KEY: Final = "score"
"""The tag the Lightning logger records a checkpoint's monitored metric value under."""

_GROUP_LABELS: Final = {"latest": "Latest", "best": "Best", "best_k": "Top-k"}
"""What each `_TAG_KEY` value is called in the list. Any other tag value is shown title-cased."""
_GROUP_ORDER: Final = tuple(_GROUP_LABELS.values())
"""The groups people look for first, in this order; any others follow alphabetically."""
_UNTAGGED_GROUP: Final = "Files"

_STEP_FORMATTER: Final = {"function": "params.value == null ? '' : String(params.value)"}
_SCORE_FORMATTER: Final = {"function": "params.value == null ? '' : Number(params.value).toPrecision(4)"}


class FileListSettings(BaseModel, frozen=True, extra="forbid"):
    """File list settings: set `key` for one artifact key, or `key_prefix` for every key under it."""

    key: str = Field(default="", description="One artifact key, e.g. `model.onnx`.")
    key_prefix: str = Field(
        default="",
        description="Every artifact key that starts with this, e.g. `checkpoints/` for all of a run's "
        "checkpoints -- including any logged later. Use instead of `key`.",
    )
    page_size: int = Field(default=10, ge=1, description="How many files each page of the list shows.")
    height: int = Field(
        default=300,
        description="Unused: the list is paged, so it grows to fit its page instead of scrolling. "
        "Kept so charts saved with a height still load.",
    )
    width: int = Field(default=560, description="Overall chart width in px.")

    @model_validator(mode="after")
    def _exactly_one_of_key_and_prefix(self) -> typing.Self:
        if bool(self.key) == bool(self.key_prefix):
            msg = "set exactly one of `key` and `key_prefix`"
            raise ValueError(msg)
        return self

    @property
    def label(self) -> str:
        """What the list is of, for messages."""
        return self.key or self.key_prefix


def _file_rows(parameters: FileListSettings, dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    """One row per logged file, in run then step order, from the wide per-key columns of a panel dataframe."""
    wanted = artifact_column(parameters.key or parameters.key_prefix)
    file_columns = [
        column
        for column in dataframe.columns
        if column.startswith(wanted)
        and (column == wanted or parameters.key_prefix)
        and not column.endswith("__tags")
    ]
    rows: list[dict[str, Any]] = []
    for column in file_columns:
        tags_column = f"{column}__tags"
        key = column.removeprefix(ARTIFACT_COLUMN_PREFIX)
        for _, file in dataframe.loc[dataframe[column].notna()].iterrows():
            raw_tags = file.get(tags_column)
            tags = cast("dict[str, str]", raw_tags) if isinstance(raw_tags, dict) else {}
            run_id = int(file[MetricColumn.RUN_ID])
            run_name = file.get(RUN_NAME_COLUMN)
            rows.append(
                {
                    "run": run_name if isinstance(run_name, str) else f"Run {run_id}",
                    "file": f"[{key.rpartition('/')[2]}]({file[column]})",
                    "step": int(file[MetricColumn.STEP]),
                    "score": _score(tags),
                    "group": _group_of(tags),
                    "run_id": run_id,
                }
            )
    return sorted(rows, key=lambda row: (row["run_id"], row["step"], row["file"]))


def _score(tags: dict[str, str]) -> float | None:
    try:
        return float(tags[_SCORE_KEY])
    except (KeyError, ValueError):
        return None


def _group_of(tags: dict[str, str]) -> str:
    """What kind of file this is, for grouping: a checkpoint's own tag, else the file kind it was logged as."""
    if tag := tags.get(_TAG_KEY):
        return _GROUP_LABELS.get(tag, tag.replace("_", " ").title())
    return tags.get(FILE_KIND_TAG, "").title() or _UNTAGGED_GROUP


def _grid(rows: list[dict[str, Any]], *, page_size: int) -> dag.AgGrid:
    has_scores = any(row["score"] is not None for row in rows)
    return dag.AgGrid(
        columnDefs=[
            column_def("run", TEXT, headerName="Run", flex=1),
            column_def("file", TEXT, headerName="File", cellRenderer="markdown", flex=2),
            column_def("step", NUMERIC, headerName="Step", valueFormatter=_STEP_FORMATTER, width=90),
            *(
                [column_def("score", NUMERIC, headerName="Score", valueFormatter=_SCORE_FORMATTER, width=100)]
                if has_scores
                else []
            ),
        ],
        rowData=rows,
        columnSize="responsiveSizeToFit",
        # `valueFormatter`s above are static JS written here, never derived from request data.
        dangerously_allow_code=True,
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": page_size,
            "paginationPageSizeSelector": sorted({page_size, 10, 25, 50}),
            "domLayout": "autoHeight",
        },
        **themed_grid_kwargs(class_name="dl-file-list"),
    )


def _group_tab(group: str, count: int) -> dmc.TabsTab:
    return dmc.TabsTab(
        dmc.Group([group, dmc.Badge(str(count), variant="light", size="xs")], gap=6, wrap="nowrap"),
        value=group,
    )


class FileListChart(ChartType[FileListSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """
    Files logged as artifacts (checkpoints, ...) by run and step, each with a download link.

    Paged and sortable, and split by what each file is -- for checkpoints the Lightning logger's own
    tags: the latest one, the best, the top-k kept -- so a long run's files stay findable.
    """

    name: typing.ClassVar[str] = "files"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[FileListSettings]:
        return FileListSettings

    @typing.override
    @classmethod
    def can_display_artifact(cls, artifact: Artifact) -> bool:
        """Files: what was logged with a file kind (`FILE_KIND_TAG`), whatever kinds a later client adds."""
        return FILE_KIND_TAG in artifact.tags

    @typing.override
    @classmethod
    def render(cls, parameters: FileListSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        rows = _file_rows(parameters, dataframe)
        if not rows:
            return dmc.Stack([dmc.Text(f"No artifacts logged for '{parameters.label}'", c="dimmed")])

        by_group: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_group.setdefault(row["group"], []).append(row)
        groups = sorted(
            by_group, key=lambda g: (_GROUP_ORDER.index(g) if g in _GROUP_ORDER else len(_GROUP_ORDER), g)
        )
        if len(groups) == 1:
            return dmc.Stack([_grid(rows, page_size=parameters.page_size)], gap="xs")
        return dmc.Stack(
            dmc.Tabs(
                [
                    dmc.TabsList([_group_tab(group, len(by_group[group])) for group in groups]),
                    *(
                        dmc.TabsPanel(
                            _grid(by_group[group], page_size=parameters.page_size), value=group, pt="xs"
                        )
                        for group in groups
                    ),
                ],
                value=groups[0],
                # Only the tab being looked at is built: each is its own table.
                keepMounted=False,
            ),
            gap="xs",
        )

    @typing.override
    @classmethod
    def hint_required_columns(cls, parameters: FileListSettings) -> set[str]:
        return set()

    @typing.override
    @classmethod
    def hint_required_artifact_keys(cls, parameters: FileListSettings) -> set[str]:
        return {parameters.key} if parameters.key else set()

    @typing.override
    @classmethod
    def hint_required_artifact_key_prefixes(cls, parameters: FileListSettings) -> set[str]:
        return {parameters.key_prefix} if parameters.key_prefix else set()

    @typing.override
    @classmethod
    def hint_required_hparams(cls, parameters: FileListSettings) -> set[str]:
        return set()

    @typing.override
    @classmethod
    def natural_width(cls, parameters: FileListSettings) -> int:
        return parameters.width

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT}


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type, and serves its stylesheet from this app only via `serve_asset`."""
    FileListChart.register(allow_override=True)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
