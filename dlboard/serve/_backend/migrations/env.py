"""
The Alembic environment dlboard's migrations run in.

Two ways this runs:
- From `_schema_upgrade.run_migrations`, with a live `connection` already open in the caller's
  transaction (`cfg.attributes["connection"]`) -- how the server upgrades its own database at
  startup.
- From the `alembic` CLI directly (e.g. `alembic revision --autogenerate`), with no connection
  attribute set -- this builds its own engine from `DLBOARD_MIGRATIONS_URL`, for a developer
  authoring a new revision against a throwaway database.

`target_metadata` is `build_metadata()` with no schema -- every revision creates unqualified
tables, and which schema they land in is up to the connection's search_path (see
`SQLStoreBase.__init__`). That keeps a revision portable across every deployment's chosen schema,
instead of baking one specific schema name into the migration scripts themselves.

`render_as_batch=True` always: sqlite can't `ALTER` a table beyond adding a column, so any future
revision doing more than that needs batch mode's copy-and-rename there regardless -- enabling it
unconditionally keeps a hand-authored revision portable across dialects instead of branching on
`context.get_bind().dialect.name`. It's a no-op on Postgres, which just runs the `ALTER` directly.

`compare_type=True` (already Alembic's own default, set explicitly so a future Alembic upgrade can't
change it under us) is what lets `alembic revision --autogenerate` and the `compare_metadata`-based
test (`migrations_test.py`) catch a column's type drifting from what a migration actually created,
not just a missing/extra table or column. `compare_server_default` stays off: sqlite's `Identity()`
primary keys reflect back with no default at all (sqlite has no real IDENTITY catalog to read), so
turning it on flags every single primary key as "drifted" the moment anything else changes. A
genuinely wrong server default is still caught at `upgrade()` time -- the database itself rejects a
default whose literal doesn't match the column's type (the `compare_metadata` test runs on a real
Postgres, where that's exactly how a past mistake here was actually found).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
from alembic import context  # pyrefly: ignore [implicit-reexport]

from dlboard.serve import sql
from dlboard.serve._backend._sql_store_base import build_metadata

if TYPE_CHECKING:
    from alembic.autogenerate.api import AutogenContext

config = context.config
metadata, _ = build_metadata()
target_metadata = metadata


def _render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """
    Render dlboard's own `UTCDateTime` by its stable public import path, not its real module.

    Autogenerate's default renderer spells a `TypeDecorator` using the module it's actually
    defined in -- the private `dlboard.serve._backend._sql` -- which a generated revision would
    then depend on forever. This renders it against `dlboard.serve.sql`'s public re-export instead
    (see `dlboard/serve/__init__.py`), the same one `0001_baseline.py` uses, so a revision survives
    `_sql.py` being refactored. `UTCDateTime.impl` is always `sa.DateTime(timezone=True)` (it's a
    class-level constant, never constructed differently), so `timezone=True` is hardcoded rather
    than read back off `obj`.
    """
    if type_ == "type" and isinstance(obj, sql.UTCDateTime):
        autogen_context.imports.add("from dlboard.serve import sql")
        return "sql.UTCDateTime(timezone=True)"
    return False


def run_migrations_online() -> None:
    """Run migrations against whichever connection this invocation was given (see module docstring)."""
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=_render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = sa.create_engine(os.environ["DLBOARD_MIGRATIONS_URL"])
    try:
        with engine.connect() as cli_connection:
            context.configure(
                connection=cli_connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                render_item=_render_item,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
