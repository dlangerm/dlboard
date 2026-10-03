"""
The Alembic environment dltrack's migrations run in.

Two ways this runs:
- From `_schema_upgrade.run_migrations`, with a live `connection` already open in the caller's
  transaction (`cfg.attributes["connection"]`) -- how the server upgrades its own database at
  startup.
- From the `alembic` CLI directly (e.g. `alembic revision --autogenerate`), with no connection
  attribute set -- this builds its own engine from `DLTRACK_MIGRATIONS_URL`, for a developer
  authoring a new revision against a throwaway database.

`target_metadata` is `build_metadata()` with no schema -- every revision creates unqualified
tables, and which schema they land in is up to the connection's search_path (see
`SQLStoreBase.__init__`). That keeps a revision portable across every deployment's chosen schema,
instead of baking one specific schema name into the migration scripts themselves.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import context

from dltrack.serve._backend._sql_store_base import build_metadata

config = context.config
metadata, _ = build_metadata()
target_metadata = metadata


def run_migrations_online() -> None:
    """Run migrations against whichever connection this invocation was given (see module docstring)."""
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = sa.create_engine(os.environ["DLTRACK_MIGRATIONS_URL"])
    try:
        with engine.connect() as cli_connection:
            context.configure(connection=cli_connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
