"""
The app's URLs, and the values in a page's query.

`relative_path` is a typed `dash.get_relative_path`: its own stubs are untyped, which strict mode rejects.
"""

from __future__ import annotations

import typing

import dash
from pydantic import BaseModel, ModelWrapValidatorHandler, ValidationError, model_validator


class UrlState(BaseModel):
    """
    A page's state as its URL (or a cookie) carries it: a value that doesn't validate costs only itself.

    A link cut short, edited by hand, or written by an older version of the page can carry one bad value. That field falls
    back to its default and every other is kept, rather than all of them: a comparison's hidden runs are not drawn again
    because the tab name beside them was bad. What still fails without them (a check across fields) is every default.
    """

    @model_validator(mode="wrap")
    @classmethod
    def _bad_values_cost_only_themselves(
        cls,
        data: object,  # dlboard-ignore[forbid-string-ignore] -- whatever the URL held
        handler: ModelWrapValidatorHandler[typing.Self],
    ) -> typing.Self:
        try:
            return handler(data)
        except ValidationError as invalid:
            if not isinstance(data, dict):
                return handler({})
            bad = {error["loc"][0] for error in invalid.errors() if error["loc"]}
            fields = typing.cast("dict[object, object]", data)
            try:
                return handler({key: value for key, value in fields.items() if key not in bad})
            except ValidationError:
                return handler({})


def relative_path(path: str) -> str:
    """
    `path`, prefixed with `DLBOARD_URL_PREFIX` if a deployment set one (see `dlboard.serve.app.app`).

    Every href/redirect this app renders or issues goes through this (never a bare literal path),
    since the browser treats a leading-`/` URL as site-root-relative regardless of the page it's on
    -- the one place that's *not* true is a route's own registration (`add_public_route`/
    `add_url_rule`), which already reads `app.config.routes_pathname_prefix` directly.
    """
    return dash.get_relative_path(path)
