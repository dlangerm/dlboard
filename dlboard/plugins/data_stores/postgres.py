"""
A Postgres metadata store, for a shared (multi-user, multi-worker) deployment.

Every setting is an environment variable (`POSTGRES_<FIELD>`, see `PostgresSettings`). psycopg is
libpq-based, so libpq's own environment (`PGPASSFILE`, `PGSERVICE`, `PGSSLCRL`, ...) and service
files keep working underneath these too, for anything not modeled here.

Each server worker process builds its own connection pool: size `pool_size + max_overflow` so that
`workers * (pool_size + max_overflow)` stays under the server's `max_connections` (or put
PgBouncer in front, and see `prepare_threshold`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, override

import sqlalchemy as sa
from pydantic import NonNegativeInt, PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.dialects import postgresql

from dlboard.serve import SQLStoreBase, set_data_store

if TYPE_CHECKING:
    from dash import Dash


_SCHEMA_LOCK_KEY: Final = int.from_bytes(b"dlboard")
"""The `pg_advisory_xact_lock` key every dlboard process takes while creating/migrating its schema."""


type SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
"""libpq's `sslmode`: how hard to insist on (and verify) TLS."""

type TargetSessionAttrs = Literal["any", "read-write", "read-only", "primary", "standby", "prefer-standby"]
"""libpq's `target_session_attrs`: which server of a multi-host `host` list to settle on."""


class PostgresSettings(BaseSettings):
    """Connection, TLS, timeout, and pool settings, read from `DLBOARD_POSTGRES_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_POSTGRES_")

    host: str = "localhost"
    """A host name, IP, or unix-socket directory -- or a comma-separated list of them, for failover."""
    port: PositiveInt = 5432
    database: str = "dlboard"
    user: str = "dlboard"
    password: SecretStr | None = None
    """Left unset, libpq falls back to `PGPASSFILE`/`~/.pgpass` (or passwordless auth)."""
    db_schema: str = "public"
    """The schema dlboard's tables live in -- created if missing -- e.g. to share one database."""
    application_name: str = "dlboard"

    sslmode: SslMode = "prefer"
    sslrootcert: Path | None = None
    sslcert: Path | None = None
    sslkey: Path | None = None
    target_session_attrs: TargetSessionAttrs | None = None
    """E.g. `read-write` with a multi-host `host`, to always land on the current primary."""

    connect_timeout_s: PositiveInt = 10
    statement_timeout_ms: NonNegativeInt | None = None
    """Server-side cap on any one statement; unset leaves the server's own default (usually none)."""
    idle_in_transaction_session_timeout_ms: NonNegativeInt | None = None
    keepalives_idle_s: PositiveInt | None = None
    """Seconds of idleness before TCP keepalives start -- for load balancers that drop idle connections."""

    pool_size: PositiveInt = 5
    max_overflow: NonNegativeInt = 10
    pool_timeout_s: PositiveFloat = 30
    pool_recycle_s: PositiveInt = 1800
    """Replace a pooled connection older than this, ahead of any proxy/server idle cutoff."""
    pool_pre_ping: bool = True
    """Check a pooled connection is alive before handing it out (survives failovers and restarts)."""
    prepare_threshold: NonNegativeInt | None = 5
    """psycopg's server-side prepared statements; set unset (`null`) behind PgBouncer's transaction pooling."""

    connect_args: dict[str, str] = {}
    """Any other libpq connection parameter (as a JSON object), e.g. `{"sslcrl": "/etc/ssl/crl.pem"}`."""

    def libpq_connect_args(self) -> dict[str, Any]:
        """Everything psycopg's `connect()` gets: libpq parameters, plus psycopg's own `prepare_threshold`."""
        # Every session in UTC, so timestamps read back with a UTC offset whatever the server's own TimeZone is.
        options = {
            "TimeZone": "UTC",
            "statement_timeout": self.statement_timeout_ms,
            "idle_in_transaction_session_timeout": self.idle_in_transaction_session_timeout_ms,
        }
        libpq = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password.get_secret_value() if self.password is not None else None,
            "application_name": self.application_name,
            "sslmode": self.sslmode,
            "sslrootcert": self.sslrootcert,
            "sslcert": self.sslcert,
            "sslkey": self.sslkey,
            "target_session_attrs": self.target_session_attrs,
            "connect_timeout": self.connect_timeout_s,
            "keepalives_idle": self.keepalives_idle_s,
            "options": " ".join(f"-c {name}={value}" for name, value in options.items() if value is not None),
        }
        return {
            **{name: str(value) for name, value in libpq.items() if value is not None},
            "prepare_threshold": self.prepare_threshold,
            **self.connect_args,
        }


class PostgresStore(SQLStoreBase[PostgresSettings]):
    """Use a Postgres database as the data store."""

    def __init__(self, settings: PostgresSettings) -> None:
        """Connect with `settings`, and create/migrate dlboard's schema in it."""
        engine = sa.create_engine(
            # Every connection parameter goes through `connect_args` -- never into the URL, which
            # would put the password in every `repr` of the engine.
            "postgresql+psycopg://",
            connect_args=settings.libpq_connect_args(),
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout_s,
            pool_recycle=settings.pool_recycle_s,
            pool_pre_ping=settings.pool_pre_ping,
        )
        super().__init__(engine, schema=settings.db_schema)

    @override
    def _insert_ignoring_conflicts(self, table: sa.Table) -> sa.Insert:
        return postgresql.insert(table).on_conflict_do_nothing()

    @override
    def _prepare_schema(self, conn: sa.Connection) -> None:
        """
        Serialize schema creation across processes, then make sure the schema itself exists.

        Every worker process starts up against the same database at once, and concurrent
        `CREATE TABLE IF NOT EXISTS` for the same table can fail on Postgres (a unique violation in
        its catalog) rather than wait -- so each takes this transaction-scoped advisory lock first,
        and the rest find the tables already there once they get it.
        """
        conn.execute(sa.select(sa.func.pg_advisory_xact_lock(_SCHEMA_LOCK_KEY)))
        conn.execute(sa.schema.CreateSchema(self._metadata.schema or "public", if_not_exists=True))

    @classmethod
    def get_or_create(cls, settings: PostgresSettings) -> PostgresStore:
        """Create."""
        return cls(settings)


def plug(app: Dash) -> None:
    """Plugin content."""
    set_data_store(app, PostgresStore.get_or_create(PostgresSettings()))
