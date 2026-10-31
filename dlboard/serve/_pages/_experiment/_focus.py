"""Following a link to one run: the experiment page draws just that run, and says so."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

import dash_mantine_components as dmc

from dlboard.serve import Icon, icon
from dlboard.serve._pages._dataframe_helpers import run_display_name
from dlboard.serve._url import relative_path

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dlboard.models import DataStore
    from dlboard.serve._pages._experiment._page_model import BasicExperimentPage
    from dlboard.serve._pages._experiment._paging import Paging


def experiment_href(experiment_id: int, *, view_id: int | None = None, run_id: int | None = None) -> str:
    """The address of an experiment's page: of a view of it, and with just one run drawn (`Paging.focus`), if given."""
    query = urlencode({k: v for k, v in (("view", view_id), ("run", run_id)) if v is not None})
    return relative_path(f"/experiment/{experiment_id}" + (f"?{query}" if query else ""))


def focus_banner(store: DataStore[...], page: BasicExperimentPage, paging: Paging) -> Component | None:
    """
    What the panel area says when it draws one run instead of the view's own: which run, and how to get the rest back.

    `None` when nothing is focused, or the focus is not a live run of this experiment (it is then ignored, as the
    charts ignore it, rather than saying so about a run that is not drawn).
    """
    if paging.focus is None or page.experiment_id is None:
        return None
    run = next(store.get_runs(page.experiment_id, limit=1, run_ids=frozenset({paging.focus})), None)
    if run is None:
        return None
    return dmc.Alert(
        dmc.Group(
            [
                dmc.Text(["Showing only ", dmc.Text(run_display_name(run), span=True, fw=600)], size="sm"),
                dmc.Anchor(
                    "Show the view's runs",
                    href=experiment_href(
                        page.experiment_id, view_id=page.id if page.owner_id is not None else None
                    ),
                    refresh=False,
                    size="sm",
                ),
            ],
            gap="md",
        ),
        icon=icon(Icon.EYE),
        color="blue",
        variant="light",
        py="xs",
        mb="xs",
        id="focus-banner",
    )
