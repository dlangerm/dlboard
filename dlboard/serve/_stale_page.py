"""
Tell a page that was loaded from an older version of the app to reload, instead of failing every callback.

A page remembers the shape of every callback (what each one reads) from when it loaded. After a
deploy -- or a dev server reloading -- an open tab still sends requests in the old shape, and Dash
answers each with a bare `IndexError` and a 500: every live-update poll of every open tab, until
someone happens to refresh. This answers such a request with a 409 instead, which `stale_page_banner.js`
turns into one "reload" prompt.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from flask import Response, request
from pydantic import BaseModel, ValidationError

from dlboard.serve._assets import AssetKind, serve_asset

if TYPE_CHECKING:
    from dash import Dash

_BANNER_JS = Path(__file__).with_name("stale_page_banner.js")


class _CallbackRequest(BaseModel):
    """The parts of a Dash callback request that say what shape the page believes the callback has."""

    output: str
    inputs: list[Any] = []
    state: list[Any] = []


def install_stale_page_guard(app: Dash) -> None:
    """Answer callback requests the app no longer has the shape of with a 409, and serve the banner script."""
    update_path = f"{app.config.routes_pathname_prefix}_dash-update-component"
    serve_asset(app, AssetKind.SCRIPT, _BANNER_JS.name, _BANNER_JS.read_bytes())

    @app.server.before_request
    def _reject_stale_callback() -> Response | None:
        if request.method != "POST" or request.path != update_path:
            return None
        try:
            sent = _CallbackRequest.model_validate(request.get_json(silent=True))
        except ValidationError:
            return None  # not a request Dash would understand either; let it say so
        # Dash ships no types for its callback registry.
        callback = cast("dict[str, Any] | None", app.callback_map.get(sent.output))
        if callback is not None:
            expected = len(cast("list[Any]", callback["inputs"])) + len(cast("list[Any]", callback["state"]))
            if expected == len(sent.inputs) + len(sent.state):
                return None
        return Response(
            '{"error": "this page is from an older version of dlboard; reload it"}',
            status=HTTPStatus.CONFLICT,
            mimetype="application/json",
        )
