"""A project's comparisons as cards on its page: yours first, then the ones shared with the project."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dash_mantine_components as dmc

from dlboard.serve import get_current_user
from dlboard.serve._pages._dash_helpers import section_label
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import ComparisonSummary, DataStore


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


def comparison_cards(store: DataStore[...], project_id: int) -> Component | None:
    """A row of cards for the project's comparisons the viewer can see, or nothing if there are none."""
    me = get_current_user().id
    comparisons = store.list_comparisons(project_id, me)
    if not comparisons:
        return None
    return dmc.Box(
        [
            section_label(f"Comparisons · {len(comparisons)}"),
            dmc.SimpleGrid(
                [_card(c, mine=c.owner_id == me) for c in comparisons],
                cols={"base": 1, "sm": 2, "lg": 3},
                spacing="md",
                mt="xs",
            ),
        ],
        mt="lg",
        mb="lg",
    )
