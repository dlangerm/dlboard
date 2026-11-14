"""
The store of the last run toggle made in the browser, which a chart plugin that `hides_runs_in_browser` listens to.

Its own module, with nothing of the page in it, so `dlboard.serve` can hand it to plugins without importing a page.
"""

from typing import Final

RUN_TOGGLE_ID: Final = "run-toggle"
"""
The last run drawn or hidden in the browser (`act_in_browser.js`): `{"action": "draw" | "hide", "run_ids": [int, ...]}`.

A chart whose type `hides_runs_in_browser` registers a clientside callback with this store's `data` as an `Input`, and
changes what it shows by it: a hidden run's series leave it, and a drawn run's come back, if it holds them. A change to
what it shows, not the whole of it, so a chart the server draws again starts from what it was drawn with.
"""
