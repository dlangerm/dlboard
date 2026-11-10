"""A project's comparisons as cards on its page, a page at a time: yours first, then the ones shared with the project."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc

from dlboard.serve import get_current_user
from dlboard.serve._pages._card_grid import PAGE_SIZE, CardGrid, Cards, CardSource
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import ComparisonSummary, DataStore
    from dlboard.serve._pages._card_grid import CardQuery


def _card(comparison: ComparisonSummary, *, mine: bool) -> Component:
    runs = f"{comparison.run_count} run{'s' if comparison.run_count != 1 else ''}"
    details = [
        runs,
        "yours" if mine else f"by {comparison.owner_name}",
        "shared" if comparison.shared else "private",
    ]
    return dmc.Anchor(
        dmc.Card(
            [
                dmc.Text(comparison.name, fw=600, truncate="end"),
                dmc.Text(" · ".join(details), size="xs", c="dimmed", mt="auto", pt="md"),
            ],
            padding="lg",
            h="100%",
        ),
        href=relative_path(f"/comparison/{comparison.id}"),
        underline="never",
        c="inherit",
        className="dl-card-link comparison-card",
        **{"aria-label": comparison.name},
    )


def _read(store: DataStore[...], project_id: int, name_contains: str, offset: int) -> Cards:
    me = get_current_user().id
    found = store.search_comparisons(
        project_id, me, name_contains=name_contains, limit=PAGE_SIZE, offset=offset
    )
    return Cards(found.total, [_card(c, mine=c.owner_id == me) for c in found.comparisons])


def _empty(_project_id: int, query: CardQuery) -> Component | None:
    """No comparison matches the filter; a project with none has no grid of them at all."""
    return dmc.Text("No comparison matches.", c="dimmed", size="sm") if query.q else None


COMPARISON_CARDS = CardSource(grid=CardGrid.COMPARISONS, label="Comparisons", read=_read, empty=_empty)
"""The project page's comparisons, the ones the viewer can see."""
