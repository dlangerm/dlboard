"""
The experiment page's persisted model: its panels, their tabs, and its settings.

Pure: no Dash and no store. `BasicExperimentPage` is what a view or the shared page *is* -- its panels and its
`DocumentSettings` -- and the functions below are every edit that can be made to its panels, as values in and
values out, for the feature modules to apply and `_page_repository.py` to save.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Any

import pandas as pd
from dash.development.base_component import Component
from pydantic import BaseModel, Field

from dlboard import models

if TYPE_CHECKING:
    from collections.abc import Mapping

UNGROUPED_TAB: typing.Final = ""
"""`PanelInstance.tab`'s default -- panels with no explicit tab all share this one implicit group."""

UNGROUPED_TAB_LABEL: typing.Final = "General"
"""What the ungrouped tab is called on screen -- and so a name no other tab can take."""


class DocumentSettings(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """
    What a view *is*, beside its panels -- typed, under the very keys a page has always stored them by.

    Changing it is an edit, and branches into a view of your own. How the page is being browsed (its tab,
    which panels are open) is not here: that is `Paging`, never saved to a view. A change is constructed
    with just the fields it sets (`DocumentSettings(excluded_runs=[3])`), and `merged_into` writes only those:
    every other key a page holds is left as it is.
    """

    excluded_runs: list[int] = Field(default_factory=list)
    """The run ids the run-comparison table has deselected, which every panel's fetch leaves out."""
    hparam_columns: list[str] = Field(default_factory=list, alias="hparam-table-selected")
    """Which hyperparameter columns the run-comparison table shows."""

    def merged_into(self, page_settings: Mapping[str, Any]) -> dict[str, Any]:
        """`page_settings` with every field this was constructed with written in."""
        return {**page_settings, **self.model_dump(mode="json", by_alias=True, exclude_unset=True)}


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

    @property
    def document(self) -> DocumentSettings:
        return DocumentSettings.model_validate(self.page_settings)

    def with_settings(self, change: DocumentSettings) -> BasicExperimentPage:
        """A copy with `change` written into its settings (see `DocumentSettings.merged_into`)."""
        return self.model_copy(update={"page_settings": change.merged_into(self.page_settings)})


class ChartLocation(typing.NamedTuple):
    """Where a chart sits on the page: the panel that holds it, and its place in that panel."""

    panel: models.PanelInstance[Any, Any]
    index: int
    chart: models.ChartInstance[Any, Any]


def locate_chart(panels: list[models.PanelInstance[Any, Any]], chart_id: str) -> ChartLocation | None:
    """Where the chart with `chart_id` is (the first, if charts of one config share an id), or `None` if it is gone."""
    return next(
        (
            ChartLocation(panel, index, chart)
            for panel in panels
            for index, chart in enumerate(panel.charts)
            if chart.id == chart_id
        ),
        None,
    )


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
    belongs to -- and so, unlike a pure reorder, always goes through a full `_page_edits.commit`
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
        name=unique_panel_name(without_chart, tab or UNGROUPED_TAB_LABEL), tab=tab, charts=[chart]
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
