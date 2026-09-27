"""Per-app state that one plugin sets once at startup and everything else reads back off that app."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from dash import get_app
from structlog.stdlib import get_logger

if TYPE_CHECKING:
    from dash import Dash

_log = get_logger(__name__)


@dataclass(frozen=True)
class AppSlot[T]:
    """
    One named piece of state stored on a `Dash` app instance (its data store, auth provider, ...).

    Always resolved against one specific app -- the one passed in, or else the app `dash.get_app()`
    says is serving the current callback/request -- and never cached process-wide: a process can
    build more than one app (every test session does), and each must only ever see its own state.
    """

    label: str

    @property
    def _attribute(self) -> str:
        return f"_dltrack_{self.label.replace(' ', '_')}_"

    def set(self, app: Dash, value: T) -> None:
        if hasattr(app, self._attribute):
            msg = f"Refusing to overwrite the already-set {self.label}."
            raise AttributeError(msg)
        _log.info("set %s to %s", self.label, type(value).__name__)
        setattr(app, self._attribute, value)

    def get(self, app: Dash | None = None) -> T:
        app = app or cast("Dash", get_app())
        if not hasattr(app, self._attribute):
            msg = f"The {self.label} was not set for this app"
            raise AttributeError(msg)
        return cast("T", getattr(app, self._attribute))

    def find(self, app: Dash) -> T | None:
        """Like `get`, but `None` when nothing set it -- for state that's optional to install."""
        return cast("T | None", getattr(app, self._attribute, None))

    def wait(self, app: Dash) -> T:
        """Like `get`, but blocks until some plugin's `plug()` sets it -- plugin order isn't guaranteed."""
        while not hasattr(app, self._attribute):
            time.sleep(1)
        return self.get(app)
