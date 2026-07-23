"""A plugin protocol."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from dash import Dash


class PluginProtocol(typing.Protocol):
    """Duck typing for plugins."""

    @classmethod
    def plug(cls, app: Dash) -> None:
        """The plug method."""
        ...
