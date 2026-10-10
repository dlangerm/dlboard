"""
Serve a static asset (script, stylesheet, font) from one `Dash` app, cached forever by the browser.

The core app and every plugin that ships its own browser-side file go through `serve_asset`, so
each asset is read exactly once (here, at registration) and served from a content-hashed URL with
an `immutable` cache header. A changed file gets a new hash, hence a new URL, so a long-lived
cache can never serve a stale copy -- and an unchanged one costs a returning browser no request
at all, not even a revalidation.

Routes hang off this app instance's own Flask server and script/css lists, never Dash's global
`hooks.route`/`hooks.script` registry, which would leak across every `Dash` app built in the same
process (see CLAUDE.md).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from flask import Response

if TYPE_CHECKING:
    from dash import Dash

_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_HASH_LENGTH = 8


class AssetKind(StrEnum):
    """What an asset is, which decides its mimetype and whether the page loads it on its own."""

    SCRIPT = "application/javascript"
    """Appended to the app's script list, so every page loads it."""

    STYLESHEET = "text/css"
    """Appended to the app's stylesheet list, so every page loads it."""

    FONT = "font/woff2"
    """Only served -- a stylesheet's `@font-face` references it by the URL `serve_asset` returns."""


def serve_asset(app: Dash, kind: AssetKind, name: str, content: bytes) -> str:
    """
    Serve `content` from `app` under a content-hashed URL derived from `name`, and return that URL.

    `name` is only the human-readable part of the URL (`line_chart_tooltip.js` is served as
    `.../line_chart_tooltip.<hash>.js`); two assets may share a name as long as their content
    differs.
    """
    digest = hashlib.sha256(content).hexdigest()[:_HASH_LENGTH]
    filename = PurePosixPath(name)
    prefix = str(app.config.routes_pathname_prefix)  # pyrefly: ignore [unknown-argument-type]
    route = f"{prefix}{filename.stem}.{digest}{filename.suffix}"
    app.server.add_url_rule(
        route,
        endpoint=route,
        view_func=lambda: Response(
            content, mimetype=kind.value, headers={"Cache-Control": _IMMUTABLE_CACHE_CONTROL}
        ),
    )
    match kind:
        case AssetKind.SCRIPT:
            app.scripts.append_script({"external_url": route, "external_only": True})
        case AssetKind.STYLESHEET:
            app.css.append_css({"external_url": route, "external_only": True})
        case AssetKind.FONT:
            pass
    return route
