"""
The app's icon set: a vendored subset of Tabler Icons (MIT, `icons/LICENSE-tabler-icons.txt`).

Every icon becomes one CSS rule in a single stylesheet generated at startup and served once via
`serve_asset` (cached forever): `.dl-icon-<name>` sets a `--dl-icon` mask image, and `.dl-icon`
paints `currentColor` through it. So an icon costs no request, no JS and no SVG in the DOM, is
colored by the text it sits in, and works anywhere a class name does -- including inside an
ag-grid cell (`icon_cell_class`), which can't hold a Dash component.

Adding one: drop its `.svg` into `icons/` and add the matching `Icon` member (a test keeps the two
in sync).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from dash import html

from dltrack.serve._assets import AssetKind, serve_asset

if TYPE_CHECKING:
    from dash import Dash

ICONS_DIR = Path(__file__).with_name("icons")

_BASE_CSS = """
.dl-icon, .dl-icon-cell::before {
  display: inline-block;
  flex-shrink: 0;
  width: 1em;
  height: 1em;
  background-color: currentColor;
  -webkit-mask: var(--dl-icon) center / contain no-repeat;
  mask: var(--dl-icon) center / contain no-repeat;
}
.dl-icon {
  vertical-align: -0.125em;
}
.dl-icon-cell::before {
  content: "";
  font-size: 1rem;
}
/* An icon-only column is typically narrower than ag-grid's own cell padding leaves room for. */
.ag-root .ag-cell.dl-icon-cell {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
}
"""


class Icon(StrEnum):
    """One vendored icon, named after its `icons/<name>.svg` file."""

    ADD = "plus"
    ADMIN = "settings"
    BREADCRUMB = "chevron-right"
    CLOSE = "x"
    COLUMNS = "columns-3"
    COPIED = "check"
    COPY = "copy"
    DARK_MODE = "moon"
    DELETE = "trash"
    DRAG = "grip-vertical"
    EDIT = "pencil"
    EXPORT = "download"
    IMPORT = "upload"
    KEY = "key"
    LIGHT_MODE = "sun"
    LINK = "link"
    LOGO = "chart-line"
    MORE = "dots"
    MOVE_TO = "arrow-right"
    NOTES = "message-circle"
    SEARCH = "search"
    SIDEBAR = "layout-sidebar"
    SIGN_OUT = "logout"
    SUGGEST = "sparkles"

    @property
    def class_name(self) -> str:
        """The class that selects this icon; pair it with `dl-icon` (or `dl-icon-cell`)."""
        return f"dl-icon-{self.value}"


def icon(name: Icon, *, size: str | None = None) -> html.Span:
    """
    An inline icon, colored like the text around it and `size` wide/tall (default `1em`).

    Decorative (`aria-hidden`): whatever it sits in carries the accessible name -- an icon-only
    button gets it from `aria-label`, see `tooltipped_action_icon`.
    """
    style = {"fontSize": size} if size else None
    return html.Span(
        className=f"dl-icon {name.class_name}", style=style, **cast("dict[str, Any]", {"aria-hidden": "true"})
    )


def icon_cell_class(name: Icon) -> str:
    """An ag-grid `cellClass` that draws `name` before the cell's (usually empty) content."""
    return f"dl-icon-cell {name.class_name}"


def install_icons(app: Dash) -> None:
    """Serve the stylesheet every `Icon` is drawn from, on `app` only."""
    rules = [
        f'.{name.class_name} {{ --dl-icon: url("data:image/svg+xml,{quote((ICONS_DIR / f"{name}.svg").read_text())}"); }}'
        for name in Icon
    ]
    serve_asset(app, AssetKind.STYLESHEET, "icons.css", (_BASE_CSS + "\n".join(rules)).encode())
