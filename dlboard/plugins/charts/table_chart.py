"""
A data table over a panel's metrics, hyperparameters and artifacts.

What a row is (`TableRows`) and what the columns show (`show_metrics`, `show_hparams`, `show_artifacts`) are
settings of their own, so one table can be any of the shapes people ask for:
- one row per run (the default): each run's hyperparameters beside its latest value of each metric -- the same
  shape as the experiment page's Runs table -- and its latest artifact under each key shown, with the tags.
- one row per value of a metric (`row_field`: `step`, `epoch`, ...): of each run in turn, or with the runs
  side by side (`spread_by_run`), a column group each. A metric logged less often than the row field belongs to
  the latest value before it, so an artifact logged mid-epoch is in that epoch's row.
- one row per logged artifact, with its tags as columns to sort and filter by -- for finding a sample among hundreds.

A run is named and colored as it is in the charts and the Runs table, and the grid keeps its sort and filters
(and page) when a live update redraws it: see `_table`.
"""

from __future__ import annotations

import hashlib
import re
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dash
import dash_ag_grid as dag
import pandas as pd
from dash import MATCH, Input, Output, State

from dlboard.models import (
    ARTIFACT_COLUMN_PREFIX,
    DEFAULT_RENDER_CONTEXT,
    HPARAM_COLUMN_PREFIX,
    RUN_NAME_COLUMN,
    ChartType,
    ColumnKind,
    MetricColumn,
    Quantity,
    RenderContext,
    TagValue,
    artifact_column,
    artifact_fname_column,
    artifact_keys_in,
    artifact_tags_column,
    is_inlineable_artifact,
)
from dlboard.plugins.charts._artifact_tags import parse_tags
from dlboard.plugins.charts._key_patterns import KeyPatterns
from dlboard.plugins.charts._table_settings import Aggregate, TableChartSettings, TableRows
from dlboard.serve import AssetKind, ClientsideScript, series_swatch_class, serve_asset
from dlboard.serve._grid import (
    DEFAULT_TABLE_FONT_SIZE,
    NUMERIC,
    TEXT,
    column_def,
    infer_column_dtype,
    themed_grid_kwargs,
)

_IDENTITY_COLS = frozenset({MetricColumn.RUN_ID, RUN_NAME_COLUMN})
_BOOKKEEPING_COLS = frozenset({*MetricColumn, RUN_NAME_COLUMN})
_NOT_METRIC_PREFIXES: typing.Final = (HPARAM_COLUMN_PREFIX, ARTIFACT_COLUMN_PREFIX)

_GRID_TYPE: typing.Final = "dl-table-chart"
"""The `type` of a table chart's grid id (`{"type": ..., "chart": <chart id>}`), for its page-restoring callback."""

_ARTIFACT_FIELD: typing.Final = "artifact"
"""The field of an artifact's link in a table of artifacts; a tag's is this and `__tag__` and its name."""

_ROW_ID_FIELD: typing.Final = "_row_id"
_SWATCH_FIELD: typing.Final = "_swatch"
"""A row's identity and a run's swatch class, as fields of its own: dash-ag-grid's JS-string parser has no `String(...)`."""

_PAGE_JS = ClientsideScript(Path(__file__).with_name("table_chart_page.js"))
_CSS_PATH = Path(__file__).with_name("table_chart.css")

_THUMBNAIL_ROW_HEIGHT: typing.Final = 64
"""A row tall enough for an image thumbnail (`table_chart.css` fits the image to it), when the table has any."""


def _table(
    column_defs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    *,
    parameters: TableChartSettings,
    context: RenderContext,
    thumbnails: bool = False,
) -> dag.AgGrid:
    """
    A grid of `rows` (each carrying its own `_ROW_ID_FIELD`) that keeps its sort and filters across redraws.

    A live update replaces the whole chart, so Dash would mount a fresh grid. It is named by the chart's
    own id and persisted per session (keyed by its columns, so a table of other columns starts clean),
    and `table_chart_page.js` puts it back on its page.
    """
    leaf_fields: list[str] = [
        leaf["field"] for column in column_defs for leaf in column.get("children", [column])
    ]
    font_size = f"{parameters.font_size}px" if parameters.font_size else DEFAULT_TABLE_FONT_SIZE
    return dag.AgGrid(
        id={"type": _GRID_TYPE, "chart": context.chart_id},
        columnDefs=column_defs,
        rowData=rows,
        # Many columns scroll sideways rather than squeezing, and every column can be filtered in place.
        defaultColDef={"minWidth": 100, "floatingFilter": True},
        columnSize="responsiveSizeToFit",
        # Keeps a row (and so its selection, were it selectable) the same row as its data changes.
        getRowId=f"params.data.{_ROW_ID_FIELD}",
        # `getRowId` and a run's swatch `cellClass` are JS expression strings -- dash-ag-grid silently
        # no-ops any string-valued JS prop unless this is set. Safe here: every such string is static and
        # written by us, never derived from user/request data.
        dangerously_allow_code=True,
        persistence=hashlib.blake2b("\0".join(leaf_fields).encode(), digest_size=5).hexdigest(),
        persisted_props=["columnState", "filterModel"],
        persistence_type="session",
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": parameters.page_size,
            "paginationPageSizeSelector": sorted({parameters.page_size, 20, 50, 100}),
            "domLayout": "autoHeight",
            # A metric is free to be named `a.b`; ag-grid would otherwise read it as the field `b` of `a`.
            "suppressFieldDotNotation": True,
            **({"rowHeight": _THUMBNAIL_ROW_HEIGHT} if thumbnails else {}),
        },
        **themed_grid_kwargs(font_size=font_size, class_name="dl-table-chart"),
    )


def _message_table(text: str, parameters: TableChartSettings, context: RenderContext) -> dag.AgGrid:
    return _table(
        [column_def("message", TEXT)],
        [{"message": text, _ROW_ID_FIELD: "message"}],
        parameters=parameters,
        context=context,
    )


def _run_names(dataframe: pd.DataFrame) -> dict[int, str]:
    """Each run's display name, for the runs the dataframe carries one for."""
    if RUN_NAME_COLUMN not in dataframe.columns:
        return {}
    named = dataframe.dropna(subset=[RUN_NAME_COLUMN]).drop_duplicates(subset=[MetricColumn.RUN_ID])
    return {
        int(run_id): str(name)
        for run_id, name in zip(named[MetricColumn.RUN_ID], named[RUN_NAME_COLUMN], strict=True)
    }


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """`frame`'s rows as plain dicts, a gap being `None` rather than NaN."""
    return typing.cast(
        "list[dict[str, Any]]", frame.astype(object).where(frame.notna(), None).to_dict(orient="records")
    )


def _run_column() -> dict[str, Any]:
    """The column naming each row's run, with its chart color as a dot before the name."""
    return {
        "field": RUN_NAME_COLUMN,
        "headerName": "Run",
        "pinned": "left",
        "sortable": True,
        "filter": True,
        "minWidth": 140,
        # Each run's chart color as a dot before its name, so the table matches the charts and the Runs table.
        "cellClass": {"function": f"params.data.{_SWATCH_FIELD}"},
    }


def _run_cells(run_id: int, names: dict[int, str]) -> dict[str, Any]:
    """What every row of a run carries: its name, swatch and row identity (the last is the caller's to refine)."""
    return {
        MetricColumn.RUN_ID: run_id,
        RUN_NAME_COLUMN: names.get(run_id, f"Run {run_id}"),
        _SWATCH_FIELD: series_swatch_class(run_id),
    }


def _column_group(title: str, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`columns` under a `title` header, or nothing at all if there are none."""
    return [{"headerName": title, "children": columns}] if columns else []


def _markdown_text(text: str) -> str:
    """`text` as a literal in a Markdown link label: a file name is whatever the client logged it as."""
    return re.sub(r"([\\\[\]()*_`<>!])", r"\\\1", text)


class _ArtifactCells(typing.NamedTuple):
    """The cells one artifact adds to a row: its link, then one per tag."""

    cells: dict[str, Any]
    tag_titles: dict[str, str]
    """The header of each tag's field in `cells` (a tag measured in a unit is shown in its base one)."""
    thumbnail: bool


def _tag_cell(value: TagValue) -> tuple[bool | float | str, str]:
    """A tag as a cell that sorts and filters as what it is, and the unit to add to its header."""
    if isinstance(value, Quantity):
        return value.base_value, f" ({value.base_unit})"
    return value, ""


def _artifact_cells(field: str, row: pd.Series[Any], key: str) -> _ArtifactCells:
    """
    The cells the artifact under `key` in a panel-dataframe `row` adds, its link in `field` and each tag in its own.

    An image a browser shows inline is a thumbnail linking to the full image; any other file is a link
    to download it, named by the file name it was logged with.
    """
    url = row[artifact_column(key)]
    logged_name = row.get(artifact_fname_column(key))
    name = logged_name if isinstance(logged_name, str) else key.rpartition("/")[2]
    thumbnail = is_inlineable_artifact(name)
    label = _markdown_text(name)
    cells: dict[str, Any] = {field: f"[![{label}]({url})]({url})" if thumbnail else f"[{label}]({url})"}
    tag_titles: dict[str, str] = {}
    for tag, value in parse_tags(row.get(artifact_tags_column(key))).items():
        tag_field = f"{field}__tag__{tag}"
        cells[tag_field], unit = _tag_cell(value)
        tag_titles[tag_field] = f"{tag}{unit}"
    return _ArtifactCells(cells, tag_titles, thumbnail)


def _artifact_columns(
    field: str, title: str, tag_titles: dict[str, str], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The columns for an artifact's link, `title`d, and its tags (those `tag_titles` names, in order)."""
    return [
        column_def(field, TEXT, headerName=title, cellRenderer="markdown", minWidth=160),
        *(
            column_def(tag_field, infer_column_dtype(rows, tag_field), headerName=tag_title)
            for tag_field, tag_title in tag_titles.items()
        ),
    ]


class _Columns(typing.NamedTuple):
    """What a table shows, as names in the panel's dataframe (the artifacts by key)."""

    metrics: list[str]
    hparams: list[str]
    artifacts: list[str]


@dataclass(frozen=True)
class _Source:
    """What a table is drawn from: the panel's dataframe, in step order, and what of it the settings pick out."""

    parameters: TableChartSettings
    context: RenderContext
    ordered: pd.DataFrame
    columns: _Columns
    names: dict[int, str]

    def latest_artifacts(self, key: str, by: list[str]) -> pd.DataFrame:
        """The last row logging an artifact under `key` of each group `by` names."""
        return self.ordered.loc[self.ordered[artifact_column(key)].notna()].groupby(by).tail(1)


def _selected_columns(parameters: TableChartSettings, dataframe: pd.DataFrame) -> _Columns:
    """The metrics, hyperparameters and artifact keys the settings pick out of what `dataframe` has."""
    columns = list(dataframe.columns)
    hparam_keys = [
        c.removeprefix(HPARAM_COLUMN_PREFIX) for c in columns if c.startswith(HPARAM_COLUMN_PREFIX)
    ]
    metric_cols = [
        c for c in columns if c not in _BOOKKEEPING_COLS and not c.startswith(_NOT_METRIC_PREFIXES)
    ]
    # `step` and the like are a metric to ask for by name, never what `*` stands for.
    bookkeeping = [c for c in MetricColumn if c in columns and c not in _IDENTITY_COLS]
    match parameters.rows:
        case TableRows.RUN:
            has_a_column_of_its_own = set[str]()
        case TableRows.FIELD:
            has_a_column_of_its_own = {parameters.row_field}
        case TableRows.ARTIFACT:
            has_a_column_of_its_own = {MetricColumn.STEP}
    metrics = KeyPatterns(tuple(parameters.show_metrics)).select(
        [*metric_cols, *bookkeeping], reserved=bookkeeping
    )
    return _Columns(
        metrics=[m for m in metrics if m not in has_a_column_of_its_own],
        hparams=[
            f"{HPARAM_COLUMN_PREFIX}{k}"
            for k in KeyPatterns(tuple(parameters.show_hparams)).select(hparam_keys)
        ],
        artifacts=KeyPatterns(tuple(parameters.show_artifacts)).select(
            artifact_keys_in(columns, "", prefix=True)
        ),
    )


def _pandas_aggregate(aggregate: Aggregate) -> str:
    match aggregate:
        case Aggregate.LATEST:
            return "last"
        case Aggregate.FIRST:
            return "first"
        case Aggregate.MIN:
            return "min"
        case Aggregate.MAX:
            return "max"
        case Aggregate.MEAN:
            return "mean"


def _reduce(source: _Source, by: list[str], columns: _Columns) -> pd.DataFrame:
    """
    One row per distinct `by`, in that order: each of `columns`' metrics reduced as the settings say, each hyperparameter as is.

    A reduction skips gaps, so a metric logged less often than the others (a per-epoch one) is its own latest
    value in a run's row, not blank because the run's last step did not log it.
    """
    reduce = _pandas_aggregate(source.parameters.aggregate)
    reducers = {
        # A metric that is not a number (a timestamp, say) has no mean or minimum worth showing.
        **{
            c: reduce if pd.api.types.is_numeric_dtype(source.ordered[c]) else "last" for c in columns.metrics
        },
        **dict.fromkeys(columns.hparams, "last"),
    }
    grouped = source.ordered.groupby(by)
    return grouped.agg(reducers).reset_index() if reducers else grouped.size().reset_index()[by]


def _render_by_group(source: _Source) -> dag.AgGrid:
    """A row per run, or per value of the row field of each run: the reduction of what the run logged for it."""
    parameters, columns = source.parameters, source.columns
    by = [MetricColumn.RUN_ID, *([parameters.row_field] if parameters.rows is TableRows.FIELD else [])]
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in _records(_reduce(source, by, columns)):
        group = tuple(record[c] for c in by)
        rows[group] = {
            **record,
            **_run_cells(int(record[MetricColumn.RUN_ID]), source.names),
            _ROW_ID_FIELD: "/".join(map(str, group)),
        }

    artifacts: list[tuple[str, dict[str, str]]] = []
    thumbnails = False
    for key in columns.artifacts:
        field = artifact_column(key)
        tag_titles: dict[str, str] = {}
        for _, artifact in source.latest_artifacts(key, by).iterrows():
            found = _artifact_cells(field, artifact, key)
            if (row := rows.get(tuple(artifact[c] for c in by))) is not None:
                row.update(found.cells)
            tag_titles |= found.tag_titles
            thumbnails |= found.thumbnail
        artifacts.append((key, tag_titles))

    shown = [*columns.metrics, *(artifact_column(key) for key, _ in artifacts)]
    if parameters.rows is TableRows.FIELD and shown:
        # A value of the row field that none of what is shown was logged under (another chart's metric) is no row.
        rows = {
            group: row for group, row in rows.items() if any(row.get(field) is not None for field in shown)
        }
    records = list(rows.values())

    artifact_title = "Latest" if parameters.rows is TableRows.RUN else "Artifact"
    column_defs = [
        _run_column(),
        *(
            [
                column_def(
                    parameters.row_field, infer_column_dtype(records, parameters.row_field), pinned="left"
                )
            ]
            if parameters.rows is TableRows.FIELD
            else []
        ),
        *_column_group(
            "Hyperparameters",
            [
                column_def(c, infer_column_dtype(records, c), headerName=c.removeprefix(HPARAM_COLUMN_PREFIX))
                for c in columns.hparams
            ],
        ),
        *_column_group(
            f"Metrics ({parameters.aggregate.value})",
            [column_def(c, infer_column_dtype(records, c)) for c in columns.metrics],
        ),
        *(
            group
            for key, tag_titles in artifacts
            for group in _column_group(
                key, _artifact_columns(artifact_column(key), artifact_title, tag_titles, records)
            )
        ),
    ]
    return _table(column_defs, records, parameters=parameters, context=source.context, thumbnails=thumbnails)


def _render_spread_by_run(source: _Source) -> dag.AgGrid:
    """A row per value of the row field, with a column group for each run: what that run logged for it."""
    parameters, columns = source.parameters, source.columns
    by = [parameters.row_field, MetricColumn.RUN_ID]

    def cell(run_id: int, field: str) -> str:
        return f"{run_id}::{field}"

    rows: dict[Any, dict[str, Any]] = {}
    for record in _records(_reduce(source, by, columns._replace(hparams=[]))):
        value = record[parameters.row_field]
        row = rows.setdefault(value, {parameters.row_field: value, _ROW_ID_FIELD: str(value)})
        for metric in columns.metrics:
            row[cell(int(record[MetricColumn.RUN_ID]), metric)] = record[metric]

    thumbnails = False
    for key in columns.artifacts:
        field = artifact_column(key)
        for _, artifact in source.latest_artifacts(key, by).iterrows():
            # A run's value of the row field is not an artifact's tags to compare: only its link is shown.
            found = _artifact_cells(field, artifact, key)
            if (row := rows.get(artifact[parameters.row_field])) is not None:
                row[cell(int(artifact[MetricColumn.RUN_ID]), field)] = found.cells[field]
            thumbnails |= found.thumbnail

    # A value of the row field that nothing shown was logged under (another chart's metric) is no row.
    records = [
        row
        for row in rows.values()
        if any(
            value is not None
            for field, value in row.items()
            if field not in {parameters.row_field, _ROW_ID_FIELD}
        )
    ]
    column_defs: list[dict[str, Any]] = [
        column_def(
            parameters.row_field,
            infer_column_dtype(records, parameters.row_field),
            pinned="left",
            minWidth=100,
        )
    ]
    for run_id in sorted({int(run_id) for run_id in source.ordered[MetricColumn.RUN_ID]}):
        leaves = [
            column_def(
                cell(run_id, metric), infer_column_dtype(records, cell(run_id, metric)), headerName=metric
            )
            for metric in columns.metrics
        ] + [
            column_def(
                cell(run_id, artifact_column(key)),
                TEXT,
                headerName=key,
                cellRenderer="markdown",
                minWidth=160,
            )
            for key in columns.artifacts
        ]
        header = {
            "headerName": source.names.get(run_id, f"Run {run_id}"),
            "headerClass": series_swatch_class(run_id),
        }
        # One value per run is the run's own column; more are grouped under it.
        column_defs.append({**leaves[0], **header} if len(leaves) == 1 else {**header, "children": leaves})
    return _table(column_defs, records, parameters=parameters, context=source.context, thumbnails=thumbnails)


def _render_artifact_rows(source: _Source) -> dag.AgGrid:
    """A row per logged artifact, beside what its run logged at the same step."""
    parameters, columns = source.parameters, source.columns
    rows: list[dict[str, Any]] = []
    tag_titles: dict[str, str] = {}
    thumbnails = False
    for key in columns.artifacts:
        logged = source.ordered.loc[source.ordered[artifact_column(key)].notna()]
        beside_cols = [*columns.metrics, *columns.hparams]
        # (A frame with no columns has no records, not an empty one per row.)
        beside: list[dict[str, Any]] = _records(logged[beside_cols]) if beside_cols else [{}] * len(logged)
        for (_, artifact), values in zip(logged.iterrows(), beside, strict=True):
            found = _artifact_cells(_ARTIFACT_FIELD, artifact, key)
            run_id, step = int(artifact[MetricColumn.RUN_ID]), int(artifact[MetricColumn.STEP])
            rows.append(
                {
                    **values,
                    **_run_cells(run_id, source.names),
                    MetricColumn.STEP: step,
                    "key": key,
                    **found.cells,
                    _ROW_ID_FIELD: f"{run_id}/{step}/{key}",
                }
            )
            tag_titles |= found.tag_titles
            thumbnails |= found.thumbnail
    if not rows:
        return _message_table("No artifacts logged under these keys yet.", parameters, source.context)

    rows.sort(key=lambda row: (row[MetricColumn.RUN_ID], row[MetricColumn.STEP], row["key"]))
    column_defs = [
        _run_column(),
        column_def(MetricColumn.STEP, NUMERIC, headerName="Step", maxWidth=110),
        *([column_def("key", TEXT, headerName="Key")] if len(columns.artifacts) > 1 else []),
        *_artifact_columns(_ARTIFACT_FIELD, "Artifact", tag_titles, rows),
        *_column_group(
            "Hyperparameters",
            [
                column_def(c, infer_column_dtype(rows, c), headerName=c.removeprefix(HPARAM_COLUMN_PREFIX))
                for c in columns.hparams
            ],
        ),
        *_column_group("Metrics", [column_def(c, infer_column_dtype(rows, c)) for c in columns.metrics]),
    ]
    return _table(column_defs, rows, parameters=parameters, context=source.context, thumbnails=thumbnails)


class TableChart(ChartType[TableChartSettings, pd.DataFrame, dag.AgGrid], frozen=True, extra="forbid"):
    """A data table over metrics, hyperparameters and artifacts."""

    name: typing.ClassVar[str] = "table"

    @classmethod
    @typing.override
    def parameter_type(cls) -> type[TableChartSettings]:
        return TableChartSettings

    @classmethod
    @typing.override
    def render(cls, parameters: TableChartSettings, dataframe: pd.DataFrame) -> dag.AgGrid:
        return cls.render_in(parameters, dataframe, DEFAULT_RENDER_CONTEXT)

    @classmethod
    @typing.override
    def render_in(
        cls, parameters: TableChartSettings, dataframe: pd.DataFrame, context: RenderContext
    ) -> dag.AgGrid:
        if dataframe.empty:
            return _message_table("No data logged yet.", parameters, context)
        ordered = (
            dataframe.sort_values([MetricColumn.RUN_ID, MetricColumn.STEP])
            if MetricColumn.STEP in dataframe.columns
            else dataframe
        )
        if parameters.rows is TableRows.FIELD:
            if parameters.row_field not in dataframe.columns:
                return _message_table(f"No {parameters.row_field!r} logged yet.", parameters, context)
            # What a run logged between two values of the row field (an artifact mid-epoch) belongs to the earlier.
            ordered = ordered.assign(
                **{parameters.row_field: ordered.groupby(MetricColumn.RUN_ID)[parameters.row_field].ffill()}
            )
        source = _Source(
            parameters, context, ordered, _selected_columns(parameters, dataframe), _run_names(dataframe)
        )
        match parameters.rows:
            case TableRows.RUN:
                return _render_by_group(source)
            case TableRows.FIELD:
                return _render_spread_by_run(source) if parameters.spread_by_run else _render_by_group(source)
            case TableRows.ARTIFACT:
                return _render_artifact_rows(source)

    @classmethod
    @typing.override
    def hint_required_columns(cls, parameters: TableChartSettings) -> set[str] | None:
        metrics = KeyPatterns(tuple(parameters.show_metrics))
        if metrics.prefixes:
            return None
        wanted = set(metrics.exact) | (
            {parameters.row_field} if parameters.rows is TableRows.FIELD else set()
        )
        # Metrics and hparams are fetched independently: a table that shows none of the former
        # (only hyperparameters, or artifacts) must not fetch every metric of the experiment.
        return wanted | {MetricColumn.STEP} if wanted else set()

    @classmethod
    @typing.override
    def hint_required_artifact_keys(cls, parameters: TableChartSettings) -> set[str]:
        return set(KeyPatterns(tuple(parameters.show_artifacts)).exact)

    @classmethod
    @typing.override
    def hint_required_artifact_key_prefixes(cls, parameters: TableChartSettings) -> set[str]:
        return set(KeyPatterns(tuple(parameters.show_artifacts)).prefixes)

    @classmethod
    @typing.override
    def hint_required_hparams(cls, parameters: TableChartSettings) -> set[str] | None:
        hparams = KeyPatterns(tuple(parameters.show_hparams))
        return None if hparams.prefixes else set(hparams.exact)

    @classmethod
    @typing.override
    def natural_width(cls, parameters: TableChartSettings) -> int:
        return parameters.width or 700

    @classmethod
    @typing.override
    def field_column_kinds(cls) -> dict[str, ColumnKind]:
        return {
            "row_field": ColumnKind.METRIC,
            "show_metrics": ColumnKind.METRIC,
            "show_hparams": ColumnKind.HPARAM,
            "show_artifacts": ColumnKind.ARTIFACT,
        }


def plug(app: dash.Dash) -> None:
    """Plugin: registers the chart type, the callback that puts a redrawn grid back on its page, and its stylesheet."""
    TableChart.register(allow_override=True)
    serve_asset(app, AssetKind.STYLESHEET, _CSS_PATH.name, _CSS_PATH.read_bytes())
    app.clientside_callback(
        _PAGE_JS.source,
        Output({"type": _GRID_TYPE, "chart": MATCH}, "paginationGoTo"),
        Input({"type": _GRID_TYPE, "chart": MATCH}, "paginationInfo"),
        State({"type": _GRID_TYPE, "chart": MATCH}, "id"),
    )
