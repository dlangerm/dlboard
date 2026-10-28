"""The chart settings form offers the experiment's real columns, and reads a chart's settings back out of its values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dash_mantine_components as dmc
import pendulum
import pytest
from pydantic import BaseModel, ValidationError

from dlboard import models
from dlboard.conftest import find_all_props, find_props, make_fake_chart
from dlboard.conftest import props as _to_props
from dlboard.models._view import ColumnKind, ParameterField, ParameterFieldType
from dlboard.plugins.charts.bar_chart import BarChart
from dlboard.plugins.charts.line_chart import LineChart
from dlboard.plugins.charts.table_chart import TableChart
from dlboard.serve._grid import CHOICE_RENDERER, SLIDER_RENDERER
from dlboard.serve._pages._experiment import _chart_settings_form as settings_form
from dlboard.serve._pages._experiment._chart_settings_form import _param_field_input, build_chart_param_form
from dlboard.serve._pages._experiment._column_catalog import KEY_PICKER_LIMIT, ColumnCatalog

if TYPE_CHECKING:
    from dlboard.plugins.data_stores.sqlite import SQLLiteStore

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")

BarChart.register(allow_override=True)
LineChart.register(allow_override=True)
TableChart.register(allow_override=True)


def _log_a_run(store: SQLLiteStore, experiment_id: int) -> models.Run:
    run = store.create_run(models.NewRun(experiment_id=experiment_id))
    store.log_metrics(
        [
            models.LoggedMetrics(
                metrics={"loss": 0.5, "acc": 0.9},
                step=0,
                experiment_id=experiment_id,
                run_id=run.id,
                timestamp_utc=_TS,
            )
        ]
    )
    store.log_hyperparams(models.NewHyperParams.from_raw(run.id, experiment_id, {"lr": 0.1}))
    return run


def _props_by_field(form: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401 -- a Dash component tree
    """The props of every settings field in `form` by its name, wherever sections nest it."""
    return {props["id"]["field"]: props for props in find_all_props(form, _is_field)}


def _is_field(props: dict[str, Any]) -> bool:
    component_id = props.get("id")
    return isinstance(component_id, dict) and component_id.get("type") == settings_form.CHART_PARAM.type


def test_build_chart_param_form_offers_the_experiments_columns(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _log_a_run(store, experiment_id)

    line = _props_by_field(
        build_chart_param_form("line", None, catalog=ColumnCatalog.load(store, experiment_id))
    )
    table = _props_by_field(
        build_chart_param_form("table", None, catalog=ColumnCatalog.load(store, experiment_id))
    )

    assert set(line["column"]["data"]) >= {"loss", "acc"}
    assert "step" in line["x_axis"]["data"]
    assert set(table["show_metrics"]["data"]) >= {"loss", "acc"}
    assert set(table["show_hparams"]["data"]) == {"*", "lr"}


def test_a_form_of_several_sections_is_one_accordion_that_opens_only_the_data_section(
    store: SQLLiteStore, experiment_id: int
) -> None:
    [accordion] = build_chart_param_form("line", None, catalog=ColumnCatalog.load(store, experiment_id))

    props = _to_props(accordion)
    items = [_to_props(item) for item in props["children"]]
    assert [item["value"] for item in items] == ["Data", "Display", "Advanced"]
    assert props["value"] == ["Data"]
    assert set(_props_by_field([accordion])) >= {"column", "x_axis", "height", "sample"}


class _FlatParams(BaseModel, frozen=True, extra="forbid"):
    note: str = ""


def test_a_form_of_one_section_has_no_accordion(store: SQLLiteStore, experiment_id: int) -> None:
    make_fake_chart("flat", _FlatParams).register(allow_override=True)

    form = build_chart_param_form("flat", None, catalog=ColumnCatalog.load(store, experiment_id))

    assert [_to_props(item)["id"]["field"] for item in form] == ["note"]


def test_a_table_takes_typed_names_and_prefixes_not_only_those_that_exist(
    store: SQLLiteStore, experiment_id: int
) -> None:
    _log_a_run(store, experiment_id)
    table = _props_by_field(
        build_chart_param_form("table", None, catalog=ColumnCatalog.load(store, experiment_id))
    )

    assert table["show_metrics"]["value"] == []
    assert table["show_artifacts"]["value"] == []


@pytest.mark.parametrize(
    ("names", "suggestions"),
    [
        pytest.param([], ["*"], id="nothing-logged"),
        pytest.param(["loss", "acc"], ["*", "acc", "loss"], id="no-folders"),
        pytest.param(
            ["eval/a", "eval/deep/b", "z"],
            ["*", "eval/*", "eval/deep/*", "eval/a", "eval/deep/b", "z"],
            id="a-prefix-for-every-folder",
        ),
        pytest.param(
            ["layer_1/g", "layer_2/g", "layer_x/g"],
            ["*", "layer_1/*", "layer_2/*", "layer_x/*", "layer_#/g", "layer_1/g", "layer_2/g", "layer_x/g"],
            id="a-digit-pattern-for-every-family-of-numbered-names",
        ),
    ],
)
def test_the_names_to_show_are_suggested_with_a_pattern_for_each_folder_and_family(
    names: list[str], suggestions: list[str]
) -> None:
    assert settings_form._pattern_suggestions(names) == suggestions


def test_merge_chart_param_values_prefers_value_over_checked() -> None:
    values = ["loss", None]
    checked = [None, True]
    field_ids = [{"field": "column"}, {"field": "sample"}]

    assert settings_form.merge_chart_param_values(values, checked, field_ids, {}) == {
        "column": "loss",
        "sample": True,
    }


def test_merge_chart_param_values_takes_a_tables_rows_from_its_data() -> None:
    entries = {"took": {"time_unit": "h"}}
    fields = {
        "tag_display": ParameterField(name="tag_display", type=ParameterFieldType.TABLE, required=False)
    }

    merged = settings_form.merge_chart_param_values(
        [], [], [], fields, table_ids=[{"field": "tag_display"}], table_values=[entries]
    )

    assert merged == {"tag_display": entries}


def test_merge_chart_param_values_drops_cleared_optional_fields() -> None:
    """A cleared NumberInput reports `None`, which isn't a valid `int` — for a field with its own
    default, that should be dropped so validation falls back to the field's default rather than
    failing with a "not a valid integer" error (regression: clearing page_size/font_size got stuck).
    """
    values = [None, None]
    checked = [None, None]
    field_ids = [{"field": "page_size"}, {"field": "column"}]
    fields = {
        "page_size": ParameterField(
            name="page_size", type=ParameterFieldType.INT, required=False, default=20
        ),
        "column": ParameterField(name="column", type=ParameterFieldType.STR, required=True),
    }

    assert settings_form.merge_chart_param_values(values, checked, field_ids, fields) == {"column": None}


# ---- build_validated_chart_instance ----


def test_build_validated_chart_instance_succeeds_for_valid_parameters() -> None:
    chart = settings_form.build_validated_chart_instance("line", {"column": "loss", "x_axis": "step"})
    assert chart.chart_type == "line"


def test_an_edited_chart_keeps_its_id_so_a_link_to_it_still_opens_it() -> None:
    parameters = {"column": "loss", "x_axis": "step"}

    kept = settings_form.build_validated_chart_instance("line", {**parameters, "height": 500}, "abc123")

    assert kept.id == "abc123"
    assert (
        settings_form.build_validated_chart_instance("line", parameters).id != kept.id
    )  # no id given: from the settings


@pytest.mark.parametrize(
    ("form_for", "height"),
    [
        pytest.param("line", 500, id="the-edited-charts-own-type"),
        pytest.param("bar", 300, id="another-type-starts-from-its-defaults"),
    ],
)
def test_the_form_is_filled_from_the_edited_chart_only_for_its_own_type(
    store: SQLLiteStore, experiment_id: int, form_for: str, height: int
) -> None:
    edited = settings_form.EditedChart(chart_type="line", parameters={"column": "loss", "height": 500})

    form = _props_by_field(
        build_chart_param_form(form_for, edited, catalog=ColumnCatalog.load(store, experiment_id))
    )

    assert form["height"]["value"] == height


def test_a_numbers_range_bounds_its_input() -> None:
    field = ParameterField(
        name="tag_precision", type=ParameterFieldType.INT, required=False, default=4, minimum=1, maximum=15
    )

    props = _to_props(_param_field_input(field.name, field, ColumnCatalog()))

    assert (props["min"], props["max"]) == (1, 15)


@pytest.mark.parametrize(
    "parameters",
    [
        # Regression: a bare `ChartInstance(...)` never validates its `parameters` (untyped dict), so a
        # required field left `None` after `settings_form.merge_chart_param_values` used to sail through and only
        # blow up later, unguarded, from `hint_required_columns`/`render` -- with no error in the UI.
        pytest.param({"column": None, "x_axis": "step"}, id="cleared-required-field"),
        # A cleared NumberInput isn't guaranteed to report `None`; `""` isn't dropped by
        # `merge_chart_param_values` (only `None` is), so it must still fail validation here.
        pytest.param({"column": "loss", "x_axis": "step", "width": ""}, id="bad-optional-field-value"),
    ],
)
def test_build_validated_chart_instance_rejects_parameters_that_would_break_later(
    parameters: dict[str, Any],
) -> None:
    """Raising here, where the add/edit-chart callback can catch it, lets it show "Invalid parameters: ..."."""
    with pytest.raises(ValidationError):
        settings_form.build_validated_chart_instance("line", parameters)


@pytest.mark.parametrize(
    ("field", "expected_type"),
    [
        (
            ParameterField(name="sample", type=ParameterFieldType.BOOL, required=False, default=True),
            dmc.Switch,
        ),
        (
            ParameterField(name="height", type=ParameterFieldType.INT, required=False, default=300),
            dmc.NumberInput,
        ),
        (
            ParameterField(
                name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.METRIC
            ),
            dmc.Select,
        ),
        (ParameterField(name="name", type=ParameterFieldType.STR, required=False), dmc.TextInput),
    ],
)
def test_param_field_input_picks_widget_by_field_type(field: ParameterField, expected_type: type) -> None:
    catalog = ColumnCatalog(metrics=("loss", "acc"))
    component = _param_field_input(field.name, field, catalog)
    assert isinstance(component, expected_type)


@pytest.mark.parametrize(
    ("required", "has_clear_button"),
    [
        # An optional numeric field (e.g. width) gets a visible clear button -- backspacing to empty
        # already works, but isn't discoverable on its own.
        pytest.param(False, True, id="optional-number"),
        pytest.param(True, False, id="required-number"),
    ],
)
def test_param_field_input_number_gets_a_clear_button_only_when_optional(
    required: bool, *, has_clear_button: bool
) -> None:
    field = ParameterField(name="width", type=ParameterFieldType.INT, required=required, default=None)
    component = _param_field_input(field.name, field, ColumnCatalog())
    props = cast("Any", component).to_plotly_json()["props"]  # pyrefly: ignore [unknown-variable-type]
    if has_clear_button:
        assert props["rightSection"] is not None
        assert props["rightSectionPointerEvents"] == "all"
    else:
        assert props.get("rightSection") is None


def test_param_field_input_offers_grouping_kind_as_a_select() -> None:
    """A bar chart's x_axis (ColumnKind.GROUPING) must be pickable, not a blind free-text field --
    otherwise a user has no way to discover a groupable column like `run_id`."""
    field = ParameterField(
        name="x_axis", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.GROUPING
    )
    component = _param_field_input(field.name, field, ColumnCatalog(metrics=("loss",), hparams=("lr",)))
    assert isinstance(component, dmc.Select)


def test_param_field_input_falls_back_to_text_when_no_columns_of_kind() -> None:
    field = ParameterField(
        name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.ARTIFACT
    )
    component = _param_field_input(field.name, field, ColumnCatalog(metrics=("loss",)))
    assert isinstance(component, dmc.TextInput)


def test_param_field_input_uses_override_over_default() -> None:
    field = ParameterField(name="sample", type=ParameterFieldType.BOOL, required=False, default=True)
    component = _param_field_input(field.name, field, ColumnCatalog(), override=False)
    # dash-mantine-components ships no py.typed marker, so pyrefly can't see this attr.
    assert cast("Any", component).to_plotly_json()["props"]["checked"] is False


def test_param_field_input_renders_fixed_choices_as_a_select() -> None:
    """A `Literal[...]`-typed field (e.g. line chart's `x_axis_type`) gets a dropdown of its fixed
    choices, taking priority over the column-kind-derived options path.
    """
    field = ParameterField(
        name="x_axis_type",
        type=ParameterFieldType.STR,
        required=False,
        default="number",
        choices=("number", "category"),
    )
    component = _param_field_input(field.name, field, ColumnCatalog(), override="category")

    assert isinstance(component, dmc.Select)
    props = cast("Any", component).to_plotly_json()["props"]  # pyrefly: ignore [unknown-variable-type]
    assert props["data"] == ["number", "category"]
    assert props["value"] == "category"


@pytest.mark.parametrize(
    ("kind", "suggested"),
    [
        pytest.param(ColumnKind.LAYER, ["boxes", "mask"], id="a-key-that-can-be-a-layer"),
        pytest.param(None, None, id="free-names"),
    ],
)
def test_a_tables_names_are_suggested_as_they_are_typed_in_the_grid(
    kind: ColumnKind | None, suggested: list[str] | None
) -> None:
    field = ParameterField(name="overlays", type=ParameterFieldType.TABLE, required=False, column_kind=kind)
    catalog = ColumnCatalog(artifacts=("boxes", "ckpt", "mask"), layers=("boxes", "mask"))

    grid = find_props(
        _param_field_input(field.name, field, catalog), settings_form._CHART_PARAM_TABLE.of("overlays")
    )

    name_column: dict[str, Any] = (grid or {})["columnDefs"][0]
    assert name_column.get("cellEditorParams", {}).get("options") == suggested


def test_a_number_with_a_range_is_a_slider_in_its_cell() -> None:
    opacity = ParameterField(
        name="opacity", type=ParameterFieldType.FLOAT, required=False, default=1.0, minimum=0, maximum=1
    )
    field = ParameterField(name="overlays", type=ParameterFieldType.TABLE, required=False, columns=(opacity,))

    grid = find_props(
        _param_field_input(field.name, field, ColumnCatalog()),
        settings_form._CHART_PARAM_TABLE.of("overlays"),
    )

    column: dict[str, Any] = (grid or {})["columnDefs"][1]
    assert column["cellRenderer"] == SLIDER_RENDERER
    assert column["cellRendererParams"] == {"min": 0, "max": 1, "step": 0.01, "fallback": 1.0}


def test_a_few_choices_are_side_by_side_in_their_cell_and_many_are_picked_from_a_list() -> None:
    few = ParameterField(
        name="visibility",
        type=ParameterFieldType.STR,
        required=False,
        default="shown",
        choices=("shown", "hidden", "off"),
    )
    many = ParameterField(name="unit", type=ParameterFieldType.STR, required=False, choices=tuple("abcdefg"))
    field = ParameterField(name="layers", type=ParameterFieldType.TABLE, required=False, columns=(few, many))

    grid = find_props(
        _param_field_input(field.name, field, ColumnCatalog()), settings_form._CHART_PARAM_TABLE.of("layers")
    )

    columns: list[dict[str, Any]] = (grid or {})["columnDefs"]
    assert columns[1]["cellRenderer"] == CHOICE_RENDERER
    assert columns[1]["cellRendererParams"] == {
        "choices": ["shown", "hidden", "off"],
        "fallback": "shown",
    }
    assert columns[2]["cellEditor"] == "agSelectCellEditor"


def test_a_row_the_chart_supplies_comes_first_by_its_label_and_is_not_the_persons_to_rename_or_remove() -> (
    None
):
    field = ParameterField(name="layers", type=ParameterFieldType.TABLE, required=False)
    saved = {"samples/mask": {"opacity": 0.3}, "truth": {"visibility": "hidden"}}

    grid = find_props(
        _param_field_input(
            field.name,
            field,
            ColumnCatalog(),
            override=saved,
            fixed_rows={"truth": "the truth", "pred": "pred"},
        ),
        settings_form._CHART_PARAM_TABLE.of("layers"),
    )

    rows: list[dict[str, Any]] = (grid or {})["rowData"]
    assert (
        rows
        == [  # the fixed rows in the order given, a blank one where nothing was saved, then the person's own
            {"key": "truth", "label": "the truth", "fixed": True, "visibility": "hidden"},
            {"key": "pred", "label": "pred", "fixed": True},
            {"key": "samples/mask", "opacity": 0.3},
        ]
    )
    name_column: dict[str, Any] = (grid or {})["columnDefs"][0]
    assert name_column["editable"] == {"function": settings_form._NOT_FIXED}
    assert (grid or {})["dashGridOptions"]["rowClassRules"] == {
        settings_form.FIXED_ROW_CLASS: settings_form._IS_FIXED
    }


@pytest.mark.parametrize("field_type", [ParameterFieldType.INT, ParameterFieldType.STR])
def test_a_number_or_a_name_typed_in_redraws_the_chart_only_once_the_typing_pauses(
    field_type: ParameterFieldType,
) -> None:
    field = ParameterField(name="setting", type=field_type, required=False)

    props = _to_props(_param_field_input(field.name, field, ColumnCatalog()))

    assert props["debounce"] == settings_form.INPUT_DEBOUNCE_MS


@pytest.mark.parametrize(
    "field",
    [
        pytest.param(
            ParameterField(
                name="column", type=ParameterFieldType.STR, required=True, column_kind=ColumnKind.METRIC
            ),
            id="one-metric",
        ),
        pytest.param(
            ParameterField(
                name="metrics",
                type=ParameterFieldType.LIST_STR,
                required=False,
                column_kind=ColumnKind.METRIC,
            ),
            id="many-metrics",
        ),
        pytest.param(
            ParameterField(
                name="show",
                type=ParameterFieldType.LIST_PATTERN,
                required=False,
                column_kind=ColumnKind.METRIC,
            ),
            id="patterns",
        ),
    ],
)
def test_a_key_picker_lists_a_bounded_number_of_names_however_many_are_logged(field: ParameterField) -> None:
    """The picker lists only this many names at once, however many are logged, so a thousand keys cannot freeze it."""
    catalog = ColumnCatalog(metrics=tuple(f"metric_{i:05}" for i in range(20 * KEY_PICKER_LIMIT)))

    component = cast("Any", _param_field_input(field.name, field, catalog))
    props: dict[str, Any] = component.to_plotly_json()["props"]

    assert props["limit"] == KEY_PICKER_LIMIT


_MANY = ColumnCatalog(metrics=tuple(f"layer_{i:04d}/grad_norm" for i in range(KEY_PICKER_LIMIT * 3)))


@pytest.mark.parametrize(
    ("field_type", "search", "keep", "first", "count"),
    [
        pytest.param(
            ParameterFieldType.STR,
            "",
            [],
            "layer_0000/grad_norm",
            KEY_PICKER_LIMIT,
            id="a-page-not-every-name",
        ),
        pytest.param(
            ParameterFieldType.STR, "0250", [], "layer_0250/grad_norm", 1, id="the-rest-are-a-search-away"
        ),
        pytest.param(
            ParameterFieldType.LIST_STR,
            "layer_000",
            ["layer_0299/grad_norm"],
            "layer_0299/grad_norm",
            11,
            id="what-it-holds-is-kept",
        ),
        pytest.param(
            ParameterFieldType.LIST_PATTERN, "#", [], "layer_#/grad_norm", 1, id="patterns-are-searched-too"
        ),
    ],
)
def test_a_key_picker_offers_a_page_of_what_its_search_finds(
    field_type: ParameterFieldType, search: str, keep: list[str], first: str, count: int
) -> None:
    """An experiment of 11,000 metrics sent each one to the browser with every picker (#416)."""
    field = ParameterField(name="column", type=field_type, required=True, column_kind=ColumnKind.METRIC)

    options = settings_form.key_picker_options(field, _MANY, search=search, keep=keep)

    assert (options[0], len(options)) == (first, count)
