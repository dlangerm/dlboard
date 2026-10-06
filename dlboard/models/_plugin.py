"""A plugin protocol."""

from __future__ import annotations

import inspect
import typing

from pydantic import BaseModel

if typing.TYPE_CHECKING:
    from dash import Dash


@typing.runtime_checkable
class PluginProtocol(typing.Protocol):
    """Duck typing for plugins."""

    @classmethod
    def plug(cls, app: Dash) -> None:
        """The plug method."""
        ...


class InstalledPlugin(BaseModel, frozen=True, extra="forbid"):
    """
    A snapshot of one plugin's identity, captured once at app startup.

    Deliberately holds no reference to the plugin module/object itself, only its name and
    description -- so introspecting the running app's plugin list (e.g. the admin page's About
    tab) can't be used to reach back into a plugin's internal state, whatever singletons or
    thread-unsafe bits it might carry, from an arbitrary callback thread.
    """

    name: str
    """The plugin's dotted module path, e.g. `dlboard.plugins.auth.anonymous`."""

    description: str | None = None
    """The first line of the plugin's module docstring, if it has one."""

    @classmethod
    def describe(cls, plugin: PluginProtocol) -> InstalledPlugin:
        """Snapshot a plugin's module name and docstring -- nothing that outlives this call."""
        module = inspect.getmodule(plugin)
        name = module.__name__ if module else repr(plugin)
        doc = inspect.getdoc(plugin)
        return cls(name=name, description=doc.splitlines()[0] if doc else None)
