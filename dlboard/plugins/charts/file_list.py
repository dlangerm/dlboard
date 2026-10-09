"""A file list with download links: one row per run and step, for artifacts logged as files."""

from __future__ import annotations

import typing

import dash_mantine_components as dmc
import pandas as pd
from pydantic import BaseModel, Field

from dlboard.models import FILE_KIND_TAG, RUN_NAME_COLUMN, Artifact, ChartType, ColumnKind, MetricColumn
from dlboard.plugins.charts._table_style import artifact_column, artifact_tags_column, format_tags_caption
from dlboard.serve import series_swatch_class

if typing.TYPE_CHECKING:
    import dash


class FileListSettings(BaseModel, frozen=True, extra="forbid"):
    """File list settings."""

    key: str
    height: int = Field(
        default=300, description="Tallest the list gets, in px, before it scrolls instead of growing."
    )
    width: int = Field(default=480, description="Overall chart width in px.")


class FileListChart(ChartType[FileListSettings, pd.DataFrame, dmc.Stack], frozen=True, extra="forbid"):
    """Every file logged under one artifact key (checkpoints, ...), by run and step, each with a download link."""

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
        col = artifact_column(parameters.key)
        if col not in dataframe.columns:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        tag_col = artifact_tags_column(parameters.key)
        has_names = RUN_NAME_COLUMN in dataframe.columns
        ordering = [MetricColumn.RUN_ID, MetricColumn.STEP]
        columns = [
            *ordering,
            col,
            *([tag_col] if tag_col in dataframe.columns else []),
            *([RUN_NAME_COLUMN] if has_names else []),
        ]
        files = (
            dataframe.loc[dataframe[col].notna(), columns]
            .drop_duplicates(subset=ordering)
            .sort_values(ordering)
        )
        if files.empty:
            return dmc.Stack([dmc.Text(f"No artifacts logged for key '{parameters.key}'", c="dimmed")])

        rows = [
            dmc.TableTr(
                [
                    dmc.TableTd(
                        dmc.Text(
                            row[RUN_NAME_COLUMN] if has_names else f"Run {row[MetricColumn.RUN_ID]}",
                            size="xs",
                            className=series_swatch_class(int(row[MetricColumn.RUN_ID])),
                        )
                    ),
                    dmc.TableTd(dmc.Text(str(row[MetricColumn.STEP]), size="xs")),
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
                    mah=parameters.height,
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
        return parameters.width

    @typing.override
    @classmethod
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {"key": ColumnKind.ARTIFACT}


def plug(app: dash.Dash) -> None:  # noqa: ARG001
    """Plugin."""
    FileListChart.register(allow_override=True)
