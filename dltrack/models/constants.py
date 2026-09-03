"""
Ids genuinely owned by `serve/app.py` (the app shell), not any single page.

An id that only one page's callbacks read/write belongs in that page's own implementation module,
typed against a page-scoped tag (`StoreId[MyPage]`, `DivId[MyPage]`, ...) via
`dltrack.models._component_ids` -- see e.g. `serve/_pages/_experiment/__init__.py`. Only put an id
here if `serve/app.py` itself owns it, or if it's a route-skeleton container that both a
`serve/_pages/x.py` routed layout and its underscore-prefixed implementation module need (and even
then, prefer declaring it in the implementation module and importing it into the routed file, as
most pages already do).
"""

from typing import Final

from dltrack.models._component_ids import AppShell, StoreId

# Route/session state, produced by app.py's routing and read by whichever page plugin the current
# route lands on -- these are the two pieces of shared state every page-scoped module can assume
# are already populated by the time its own callbacks run.
STATE_PROJECT_ID: StoreId[AppShell] = StoreId("project-id-state")
STATE_EXPERIMENT_ID: StoreId[AppShell] = StoreId("experiment-id-state")

MANTINE_PROVIDER_ID: Final = "mantine-provider"
NAVBAR_ID: Final = "navbar"
NAVBAR_CONTENT_ID = "navbar-content"
NAVBAR_COLLAPSED_STORE_ID = "navbar-collapsed-store"
NAVBAR_COLLAPSE_TOGGLE_ID = "navbar-collapse-toggle"
NAVBAR_WIDTH_STORE_ID: Final = "navbar-width-store"
NAVBAR_RESIZE_HANDLE_ID: Final = "navbar-resize-handle"
# app.py's navbar renders this placeholder for whichever page wants to fill it -- currently only
# the experiment page (`serve/_pages/_experiment/__init__.py`) does.
NAVBAR_RUN_LIST_ID: Final = "navbar-run-list"
HEADER_USER_INDICATOR_ID: Final = "header-user-indicator"

LOCATION_ID: Final = "location"
PAGE_BREADCRUMB_ID: Final = "page-breadcrumb"
