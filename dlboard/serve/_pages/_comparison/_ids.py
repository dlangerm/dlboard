"""The ids the parts of a comparison page share."""

from __future__ import annotations

from dlboard.serve._component_ids import ButtonId, StoreId, ValueId


class ComparisonPage:
    """Page tag: marks a component id as belonging to the comparison page."""


COMPARISON_ID_STORE: StoreId[ComparisonPage] = StoreId("comparison-id")
"""The comparison the page shows, by its page id: what its callbacks act on, whoever it was before an edit forked it."""

ADD_RUNS_OPEN_ID: ButtonId[ComparisonPage] = ButtonId("comparison-add-open")
"""The button in the sidebar that opens the modal to add runs in."""

COMPARISON_GRID_ID: ValueId[ComparisonPage] = ValueId("comparison-members-grid")
"""The grid of the comparison's runs, in the sidebar."""

MEMBERS_REVISION_ID: StoreId[ComparisonPage] = StoreId("comparison-members-revision")
"""
When the comparison's runs last changed (a run added or taken out), so the sidebar's list draws them again.

Nothing is saved to the page by such a change, so there is nothing else for the list to notice it by.
"""

MEMBERS_SIGNATURE_ID: StoreId[ComparisonPage] = StoreId("comparison-members-signature")
"""A digest of what the sidebar's list was drawn from, so that it is drawn again only when that changes."""
