"""A typed `dash.get_relative_path` -- its own type stubs are untyped, which strict mode rejects."""

from __future__ import annotations

import dash


def relative_path(path: str) -> str:
    """
    `path`, prefixed with `DLTRACK_URL_PREFIX` if a deployment set one (see `dltrack.serve.app.app`).

    Every href/redirect this app renders or issues goes through this (never a bare literal path),
    since the browser treats a leading-`/` URL as site-root-relative regardless of the page it's on
    -- the one place that's *not* true is a route's own registration (`add_public_route`/
    `add_url_rule`), which already reads `app.config.routes_pathname_prefix` directly.
    """
    return dash.get_relative_path(path)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
