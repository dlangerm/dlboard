"""
The Columns picker of a run list: the hyperparameters and metrics to show as columns, picked and then applied with a button.

Changes only take effect on "Apply", not per tick: drawing the list reads a hyperparameter and metric value for each run, so
committing it once per intended change keeps a multi-column edit from a burst of reads. "Apply" only appears once the picked
columns differ from what is applied (a clientside callback compares against the `applied` store's baseline).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import dash_mantine_components as dmc
from dash import Input, Output, State
from dash.dcc import Store

from dlboard.serve import ClientsideScript, Icon
from dlboard.serve._pages._dash_helpers import tooltipped_action_icon
from dlboard.serve._pages._experiment._column_catalog import KEY_PICKER_LIMIT, first_matches

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dash import Dash
    from dash.development.base_component import Component

_COLS_CHANGED_JS = ClientsideScript(Path(__file__).with_name("hparam_cols_changed.js"))


class ColumnsPickerIds(NamedTuple):
    """The ids of one run list's picker: its page draws one, and each is told apart by them."""

    select: str
    confirm: str
    applied: str
    toggle: str


def column_options(
    hparam_keys: Sequence[str], metric_keys: Sequence[str], *, search: str = "", keep: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """
    The picker's options: the first `KEY_PICKER_LIMIT` keys `search` finds, hyperparameters first, and `keep`.

    Never every name: an experiment of 11,000 metrics would send each of them to the browser (#416); the rest are a
    search away (a callback that answers what is typed). `keep` is what the picker holds, so it keeps its picks.
    """
    hparams = first_matches(hparam_keys, search)
    metrics = first_matches(metric_keys, search, KEY_PICKER_LIMIT - len(hparams))
    kept_hparams = [key for key in keep if key in hparam_keys and key not in hparams]
    kept_metrics = [key for key in keep if key not in hparam_keys and key not in metrics]
    return [
        {"group": "Hyperparameters", "items": [*kept_hparams, *hparams]},
        {"group": "Metrics (latest)", "items": [*kept_metrics, *metrics]},
    ]


def columns_picker(
    picker: ColumnsPickerIds, hparam_keys: Sequence[str], metric_keys: Sequence[str], applied: list[str]
) -> list[Component]:
    """The icon that opens the picker, and the store holding the columns applied, to put in a list's header."""
    return [
        dmc.Popover(
            [
                dmc.PopoverTarget(
                    tooltipped_action_icon(Icon.COLUMNS, component_id=picker.toggle, label="Choose columns")
                ),
                dmc.PopoverDropdown(
                    dmc.Group(
                        [
                            dmc.MultiSelect(
                                id=picker.select,
                                data=column_options(hparam_keys, metric_keys, keep=applied),
                                value=applied,
                                limit=KEY_PICKER_LIMIT,
                                searchable=True,
                                clearable=True,
                                size="xs",
                                style={"flex": 1, "minWidth": 220},
                                # Render this dropdown inline (not its own portal) so a click on one of its options still
                                # lands inside the outer Popover's own DOM subtree -- otherwise the Popover's own "click
                                # outside closes it" detection sees the option click as outside and closes before the
                                # selection is ever applied.
                                comboboxProps={"withinPortal": False},
                            ),
                            dmc.Button(
                                "Apply",
                                id=picker.confirm,
                                size="xs",
                                style={"display": "none", "flexShrink": 0},
                            ),
                        ],
                        align="center",
                        gap="xs",
                        wrap="nowrap",
                    )
                ),
            ],
            position="bottom-end",
            withinPortal=True,
        ),
        Store(id=picker.applied, data=applied),
    ]


def register_columns_picker(app: Dash, picker: ColumnsPickerIds) -> None:
    """Show the picker's Apply button once what is picked differs from what is applied."""
    # Compared client-side, not through a server callback per keystroke: it is instant, and one less source of rerenders.
    app.clientside_callback(
        _COLS_CHANGED_JS.source,
        Output(picker.confirm, "style"),
        Input(picker.select, "value"),
        State(picker.applied, "data"),
        prevent_initial_call=True,
    )
