"""
The app's URLs, and the values in a page's query.

`relative_path` is a typed `dash.get_relative_path`: its own stubs are untyped, which strict mode rejects.
"""

from __future__ import annotations

import dash


def split_commas[T](value: str | T) -> list[str] | T:
    """
    A comma-separated query value (`?compare=3,7`) as its items, for a `BeforeValidator` to hand on.

    A value that isn't a string -- a model built in code, with the items already apart -- is left for validation to judge.
    """
    return [item for item in value.split(",") if item] if isinstance(value, str) else value


def relative_path(path: str) -> str:
    """
    `path`, prefixed with `DLBOARD_URL_PREFIX` if a deployment set one (see `dlboard.serve.app.app`).

    Every href/redirect this app renders or issues goes through this (never a bare literal path),
    since the browser treats a leading-`/` URL as site-root-relative regardless of the page it's on
    -- the one place that's *not* true is a route's own registration (`add_public_route`/
    `add_url_rule`), which already reads `app.config.routes_pathname_prefix` directly.
    """
    return dash.get_relative_path(path)
