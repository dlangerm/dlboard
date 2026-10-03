"""Bring a database's schema to the latest revision, using the `migrations/` Alembic environment."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config

if TYPE_CHECKING:
    import sqlalchemy as sa

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations(connection: sa.Connection) -> None:
    """
    Upgrade `connection`'s database to the latest schema revision, in `connection`'s transaction.

    Alembic runs through `migrations/env.py`, which hands the diff/apply engine this same
    connection (via `cfg.attributes["connection"]`) instead of opening one of its own -- so this
    participates in whatever transaction the caller already has open.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")
