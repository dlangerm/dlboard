"""
The list of runs beside a page's charts: what the sidebar of an experiment shows, and every page of a project's, the same way.

A run has an eye, to draw it in the charts or not, and a checkbox, to select it; the selected runs are what the bar above the
list acts on (`RunAction`). A page draws its list into the sidebar itself, and listens to the request store the list's buttons
write (`request_store_id`).
"""

from dlboard.serve._pages._run_list._bar import action_bar, register_run_list, simple_action
from dlboard.serve._pages._run_list._ids import RunListSurface, request_store_id
from dlboard.serve._pages._run_list._model import RunAction, RunActionRequest

__all__ = [
    "RunAction",
    "RunActionRequest",
    "RunListSurface",
    "action_bar",
    "register_run_list",
    "request_store_id",
    "simple_action",
]
