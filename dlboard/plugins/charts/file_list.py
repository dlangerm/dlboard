"""A file list with download links: one row per run and step, for artifacts a browser can't show inline."""

from __future__ import annotations

import typing

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel

from dlboard.models import RUN_NAME_COLUMN, ChartType, ColumnKind
from dlboard.plugins.charts._table_style import artifact_column, artifact_tags_column, format_tags_caption
from dlboard.serve import series_swatch_class

if typing.TYPE_CHECKING:
    import dash

_NATURAL_WIDTH = 480
_MAX_HEIGHT_PX = 300


class FileListSettings(BaseModel, frozen=True, extra="forbid"):
    """File list settings."""

    key: str


class FileListChart(ChartType[FileListSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """Every file logged under one artifact key (checkpoints, ...), by run and step, each with a download link."""

    name: typing.ClassVar[str] = "files"

    @typing.override
    @classmethod
    def parameter_type(cls) -> type[FileListSettings]:
        return FileListSettings

    @typing.override
    @classmethod
    def render(cls, parameters: FileListSettings, dataframe: pd.DataFrame) -> dmc.Stack:
        col = artifact_column(parameters.key)
        if col not in dataframe.columns:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        tag_col = artifact_tags_column(parameters.key)
        has_names = RUN_NAME_COLUMN in dataframe.columns
        columns = [
            "run_id",
            "step",
            col,
            *([tag_col] if tag_col in dataframe.columns else []),
            *([RUN_NAME_COLUMN] if has_names else []),
        ]
        files = (
            dataframe.loc[dataframe[col].notna(), columns]
            .drop_duplicates(subset=["run_id", "step"])
            .sort_values(["run_id", "step"])
        )
        if files.empty:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        rows = [
            dmc.TableTr(
                [
                    dmc.TableTd(
                        dmc.Text(
                            row[RUN_NAME_COLUMN] if has_names else f"Run {row['run_id']}",
                            size="xs",
                            className=series_swatch_class(int(row["run_id"])),
                        )
                    ),
                    dmc.TableTd(dmc.Text(str(row["step"]), size="xs")),
                    dmc.TableTd(dmc.Text(format_tags_caption(row.get(tag_col)), size="xs", c="dimmed")),
                    dmc.TableTd(dmc.Anchor("Download", href=row[col], refresh=True, size="xs")),
                ]
            )
            for _, row in files.iterrows()
        ]
        header = dmc.TableTr([dmc.TableTh(label) for label in ("Run", "Step", "Details", "File")])
        return dmc.Stack(
            [
                dmc.ScrollArea(
                    dmc.Table(
                        [dmc.TableThead(header), dmc.TableTbody(rows)],
                        stickyHeader=True,
                        highlightOnHover=True,
                    ),
                    mah=_MAX_HEIGHT_PX,
                )
            ],
            gap="xs",
        )

    @typing.override
    @classmethod
    def hint_required_columns(cls, parameters: FileListSettings) -> set[str]:
        return set()

    @typing.override
    @classmethod
    def hint_required_artifact_keys(cls, parameters: FileListSettings) -> set[str]:
        return {parameters.key}

    @typing.override
    @classmethod
    def hint_required_hparams(cls, parameters: FileListSettings) -> set[str]:
        return set()

    @typing.override
    @classmethod
    def natural_width(cls, parameters: FileListSettings) -> int:
        return _NATURAL_WIDTH

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT}


def plug(app: dash.Dash) -> None:  # noqa: ARG001
    """Plugin."""
    FileListChart.register(allow_override=True)
