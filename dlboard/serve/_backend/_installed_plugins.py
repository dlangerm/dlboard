"""The app's installed-plugin snapshot, set once by `dlboard.serve.app.app` for the admin page to show."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dlboard.serve._backend._app_slot import AppSlot

if TYPE_CHECKING:
    from dlboard.models import InstalledPlugin

INSTALLED_PLUGINS: AppSlot[list[InstalledPlugin]] = AppSlot("installed plugins")

set_installed_plugins = INSTALLED_PLUGINS.set
get_installed_plugins = INSTALLED_PLUGINS.get
