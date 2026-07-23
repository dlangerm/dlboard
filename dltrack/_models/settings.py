"""Settings for the application."""

from __future__ import annotations

import typing
from pathlib import Path  # noqa: TC003

from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    """Environment variables."""

    store_type: typing.Literal["sqllite"] = "sqllite"
    """The store type"""

    sqllite_location: Path | None = None
    """The path to the sqllite storage location."""
