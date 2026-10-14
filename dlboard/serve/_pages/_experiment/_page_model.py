"""
The experiment page's persisted model: its panels, their tabs, and what it remembers about how it is being viewed.

Pure: no Dash and no store. `BasicExperimentPage` is what a view or the shared page *is*; the functions below are
every edit that can be made to its panels, as values in and values out, for the feature modules to apply and
`_page_repository.py` to save.
"""

from __future__ import annotations

import typing
from typing import Any, cast

import pandas as pd
from dash.development.base_component import Component

from dlboard import models

# --- `page_settings` keys ---
OPEN_PANEL_KEY: typing.Final = "open_panel"
"""Which panels the viewer has open, by name (a bare name, in pages saved before more than one could be)."""
ACTIVE_TAB_KEY: typing.Final = "active_tab"
"""Which tab the viewer last selected (`UNGROUPED_TAB` for the ungrouped one)."""
EXCLUDED_RUNS_KEY: typing.Final = "excluded_runs"
"""The run ids the run-comparison table has deselected, which `fetch_panel_dataframe` leaves out of every panel's
fetch."""
SELECTED_HPARAM_COLS_KEY: typing.Final = "hparam-table-selected"
"""Which hyperparameter columns the run-comparison table shows."""

UNGROUPED_TAB: typing.Final = ""
"""`PanelInstance.tab`'s default -- panels with no explicit tab all share this one implicit group."""


def group_panels_by_tab(
    panels: list[models.PanelInstance[Any, Any]],
) -> dict[str, list[models.PanelInstance[Any, Any]]]:
    """Group panels by `tab`, preserving first-seen tab order (the ungrouped tab always sorts first)."""
    groups: dict[str, list[models.PanelInstance[Any, Any]]] = {UNGROUPED_TAB: []}
    for p in panels:
        groups.setdefault(p.tab, []).append(p)
    return groups


class BasicExperimentPage(models.Page[pd.DataFrame, Component], frozen=True, extra="forbid"):
    """Basic experiment page: per-panel charts, grouped into tabs."""

    def open_panels(self) -> list[str]:
        """Names of the panels the viewer has open (the first panel's, until they open or close any)."""
        default: list[str] = [self.panels[0].name] if self.panels else []
        open_value = self.page_settings.get(OPEN_PANEL_KEY, default)
        return [open_value] if isinstance(open_value, str) else cast("list[str]", open_value)

    def active_tab(self) -> str:
        """The tab being shown: the one last selected, if it still has a panel; otherwise the first."""
        tabs = group_panels_by_tab(self.panels)
        selected = self.page_settings.get(ACTIVE_TAB_KEY)
        return selected if isinstance(selected, str) and selected in tabs else next(iter(tabs))


def upsert_chart(
    panel: models.PanelInstance[Any, Any],
    panel_name: str,
    index: int | None,
    chart: models.ChartInstance[Any, Any],
) -> models.PanelInstance[Any, Any]:
    """Add `chart` to `panel` (if index is None) or replace the chart at `index`."""
    if panel.name != panel_name:
        return panel
    if index is None:
        return panel.model_copy(update={"charts": [*panel.charts, chart]})
    new_charts = list(panel.charts)
    new_charts[index] = chart
    return panel.model_copy(update={"charts": new_charts})


def add_chart_to_panel_by_name(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, chart: models.ChartInstance[Any, Any]
) -> list[models.PanelInstance[Any, Any]]:
    """Append `chart` to the panel named `panel_name`, creating that panel if it doesn't exist yet."""
    for i, p in enumerate(panels):
        if p.name == panel_name:
            new_panels = list(panels)
            new_panels[i] = p.model_copy(update={"charts": [*p.charts, chart]})
            return new_panels
    return [*panels, models.PanelInstance(name=panel_name, charts=[chart])]


def reorder_panel(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, target_name: str, *, after: bool
) -> list[models.PanelInstance[Any, Any]]:
    """Move the panel named `panel_name` to sit just before/after the panel named `target_name`."""
    if panel_name == target_name:
        return panels
    moving = next((p for p in panels if p.name == panel_name), None)
    if moving is None or not any(p.name == target_name for p in panels):
        return panels
    remaining = [p for p in panels if p.name != panel_name]
    target_idx = next(i for i, p in enumerate(remaining) if p.name == target_name)
    remaining.insert(target_idx + 1 if after else target_idx, moving)
    return remaining


def set_panel_sync(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, *, sync: bool
) -> list[models.PanelInstance[Any, Any]]:
    return [p.model_copy(update={"sync": sync}) if p.name == panel_name else p for p in panels]


def update_panel(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, changes: dict[str, Any]
) -> list[models.PanelInstance[Any, Any]]:
    """Apply `changes` (already-validated `PanelInstance` field values) to the panel named `panel_name`."""
    return [p.model_copy(update=changes) if p.name == panel_name else p for p in panels]


def move_index[T](items: list[T], index: int, target_index: int, *, after: bool) -> list[T]:
    """Move the item at `index` to sit just before/after `target_index`; a no-op if either is out of range."""
    if index == target_index or not (0 <= index < len(items)) or not (0 <= target_index < len(items)):
        return items
    items = list(items)
    item = items.pop(index)
    # `target_index` is the target's index in the *original* list -- popping `item` out from before
    # it shifts it left by one, so account for that before adding the requested offset.
    insert_at = target_index + (1 if after else 0) - (1 if index < target_index else 0)
    items.insert(max(0, min(insert_at, len(items))), item)
    return items


def reorder_chart(
    panels: list[models.PanelInstance[Any, Any]],
    panel_name: str,
    index: int,
    target_index: int,
    *,
    after: bool,
) -> list[models.PanelInstance[Any, Any]]:
    """Move the chart at `index` in the panel named `panel_name` to sit just before/after `target_index`."""
    new_panels: list[models.PanelInstance[Any, Any]] = []
    for p in panels:
        if p.name != panel_name:
            new_panels.append(p)
            continue
        new_panels.append(
            p.model_copy(update={"charts": move_index(p.charts, index, target_index, after=after)})
        )
    return new_panels


def panel_name_taken(
    panels: list[models.PanelInstance[Any, Any]], name: str, *, ignore: str | None = None
) -> bool:
    """Whether `name` is already used by a panel other than `ignore` (the panel being renamed, if any)."""
    return any(p.name == name for p in panels if p.name != ignore)


def unique_panel_name(panels: list[models.PanelInstance[Any, Any]], base: str) -> str:
    """`base`, or `"{base} (2)"`, `"{base} (3)"`, ... -- whichever isn't already a panel name."""
    if not panel_name_taken(panels, base):
        return base
    n = 2
    while panel_name_taken(panels, f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def _pop_chart(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, index: int
) -> tuple[models.ChartInstance[Any, Any] | None, list[models.PanelInstance[Any, Any]]]:
    """
    Remove the chart at `index` in `panel_name`.

    Returns `(None, panels)` unchanged if `panel_name`/`index` don't resolve to a real chart --
    shared by every "move this chart somewhere else" mutation (`move_chart_to_tab`,
    `move_chart_to_panel`) so each only has to say *where* the chart lands, not how to detach it.
    """
    source = next((p for p in panels if p.name == panel_name), None)
    if source is None or not (0 <= index < len(source.charts)):
        return None, panels
    chart = source.charts[index]
    without_chart = [
        p.model_copy(update={"charts": [c for i, c in enumerate(p.charts) if i != index]})
        if p.name == panel_name
        else p
        for p in panels
    ]
    return chart, without_chart


def move_chart_to_tab(
    panels: list[models.PanelInstance[Any, Any]], panel_name: str, index: int, tab: str
) -> list[models.PanelInstance[Any, Any]]:
    """
    Move the chart at `index` in `panel_name` into `tab`'s first panel, creating one if `tab` is empty.

    Unlike `reorder_chart` (same panel, different position), this can change which panel a chart
    belongs to -- and so, unlike a pure reorder, always goes through a full `mutate_panels_and_rerender`
    at the call site rather than a client-side splice, since the destination panel may not even be
    open (or may not exist yet).
    """
    chart, without_chart = _pop_chart(panels, panel_name, index)
    if chart is None:
        return panels
    dest = next((p for p in without_chart if p.tab == tab), None)
    if dest is not None:
        return [
            p.model_copy(update={"charts": [*p.charts, chart]}) if p.name == dest.name else p
            for p in without_chart
        ]
    new_panel = models.PanelInstance(
        name=unique_panel_name(without_chart, tab or "General"), tab=tab, charts=[chart]
    )
    return [*without_chart, new_panel]


def move_chart_to_panel(  # noqa: PLR0913
    panels: list[models.PanelInstance[Any, Any]],
    panel_name: str,
    index: int,
    target_panel: str,
    target_index: int | None,
    *,
    after: bool,
) -> list[models.PanelInstance[Any, Any]]:
    """
    Move the chart at `index` in `panel_name` into `target_panel`, at `target_index` there.

    `target_index=None` appends instead of inserting at a position -- the drop landed on the target
    panel's body generally (an empty panel, or anywhere that isn't one of its existing charts)
    rather than on a specific chart to land next to. `panel_name == target_panel` degrades to a plain
    `reorder_chart` (same-panel move, client-splice-friendly) rather than repeating that logic here.
    """
    if panel_name == target_panel:
        return (
            panels
            if target_index is None
            else reorder_chart(panels, panel_name, index, target_index, after=after)
        )
    chart, without_chart = _pop_chart(panels, panel_name, index)
    if chart is None or not any(p.name == target_panel for p in without_chart):
        return panels

    def _insert(p: models.PanelInstance[Any, Any]) -> models.PanelInstance[Any, Any]:
        if p.name != target_panel:
            return p
        if target_index is None:
            return p.model_copy(update={"charts": [*p.charts, chart]})
        new_charts = list(p.charts)
        insert_at = max(0, min(target_index + (1 if after else 0), len(new_charts)))
        new_charts.insert(insert_at, chart)
        return p.model_copy(update={"charts": new_charts})

    return [_insert(p) for p in without_chart]
