# Storage plugins

## What is this

Storage is split into two protocols, both in `dltrack/models/_data_store.py`:

- **`DataStore`** — structured metadata: users, projects, experiments, runs, metrics,
  hyperparameters, artifact *references*, soft-delete/restore/purge, audit log.
- **`ArtifactStore`** — the artifact *blobs* themselves: `log_artifacts`, `download_artifact`,
  `delete_artifact`.

They're separate on purpose — metadata and blob storage scale and fail independently, and a
deployment might reasonably want e.g. Postgres for metadata and S3 for blobs without those being
the same plugin.

## When you'd need this

You're deploying somewhere the built-in backends don't fit — e.g. S3/GCS instead of local
filesystem for artifacts. The built-ins are `dltrack/plugins/data_stores/sqlite.py` and
`filesystem.py` (what `LOCAL_DEPLOYMENT` uses) and `postgres.py`, below.

## Postgres

`dltrack.plugins.POSTGRES_STORAGE` (`postgres` + `filesystem` + the artifact purge worker) swaps
sqlite for a shared Postgres. Install the driver with the `postgres` extra
(`pip install 'dltrack[postgres]'`), put `POSTGRES_STORAGE` in your own plugin list, and run it with
`dltrack serve custom --plugins yourmodule:PLUGINS`:

```python
from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, POSTGRES_STORAGE, themes

PLUGINS = [*POSTGRES_STORAGE, my_auth_plugin, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Every setting is a `POSTGRES_*` environment variable. The full list, with docs, is
`PostgresSettings` in `dltrack/plugins/data_stores/postgres.py`:

| Concern | Variables |
|---|---|
| Connection | `POSTGRES_HOST` (a comma-separated list fails over between hosts), `_PORT`, `_DATABASE`, `_USER`, `_PASSWORD`, `_DB_SCHEMA` (created if missing), `_APPLICATION_NAME`, `_TARGET_SESSION_ATTRS` (e.g. `read-write`) |
| TLS | `POSTGRES_SSLMODE` (default `prefer`; use `verify-full` in production), `_SSLROOTCERT`, `_SSLCERT`, `_SSLKEY` |
| Timeouts | `POSTGRES_CONNECT_TIMEOUT_S`, `_STATEMENT_TIMEOUT_MS`, `_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS`, `_KEEPALIVES_IDLE_S` |
| Pooling | `POSTGRES_POOL_SIZE`, `_MAX_OVERFLOW`, `_POOL_TIMEOUT_S`, `_POOL_RECYCLE_S`, `_POOL_PRE_PING` |
| PgBouncer | `POSTGRES_PREPARE_THRESHOLD=null` disables server-side prepared statements for transaction pooling |
| Extensions | `POSTGRES_EXTENSIONS` — a JSON list created at startup if missing, e.g. `["vector"]` (needs the privilege; leave empty if a DBA manages them) |
| Anything else | `POSTGRES_CONNECT_ARGS` — a JSON object of extra libpq parameters, e.g. `{"sslcrl": "..."}` |

The driver is libpq-based, so libpq's own environment (`PGPASSFILE`, `PGSERVICE`, ...) still works
underneath these. Each server worker process has its own pool, so keep
`workers × (pool_size + max_overflow)` under the server's `max_connections`. Workers starting at
once take turns creating/migrating the schema, under an advisory lock.

## How do I build one

Implement the protocol (`DataStore` or `ArtifactStore`, or both) and register the instance from
`plug(app)`:

```python
def plug(app: Dash) -> None:
    set_data_store(app, MyStore.get_or_create(...))       # dltrack.serve._backend._data_store
    # or, for a blob store:
    set_artifact_store(app, MyArtifactStore.get_or_create(...))
```

You don't have to implement `DataStore` from scratch — `SQLStoreBase`
(`dltrack/serve/_backend/_sql_store_base.py`) already implements the full `DataStore` contract
(CRUD, cascading soft-delete, audit log, scopes) on SQLAlchemy Core. It generates every table from
its pydantic model and writes every query as a Core expression, so column types, identity columns,
quoting and bind style all come from SQLAlchemy's dialect for your database. A new SQL-backed store
only needs to subclass `SQLStoreBase`, pass `super().__init__()` an `sqlalchemy.Engine` (your
connection settings and pooling live there), and implement `_insert_ignoring_conflicts` — an
`INSERT ... ON CONFLICT DO NOTHING`, which each dialect spells through its own
`sqlalchemy.dialects.<name>.insert`, and set its `dialect`. See `plugins/data_stores/sqlite.py` for the
reference implementation, and publish it with `set_sql_store` as well as `set_data_store` (see below).

An `ArtifactStore` has no equivalent base class — `plugins/data_stores/filesystem.py` is the
reference implementation (local disk, `protocol = "file"`) to model a new one on. Configuration
(bucket name, credentials, root path) should come from `pydantic_settings.BaseSettings`, read
inside `plug()` — see either built-in for the pattern.

Read from elsewhere in the app with `get_data_store()` / `get_artifact_store()`
(`dltrack/serve/_backend/_data_store.py`) rather than passing the store instance around directly.

## Keeping a plugin's own data in the store's database

A plugin that needs its own tables (embeddings, annotations, anything `DataStore` doesn't model)
can put them in the same database as dltrack's, using features that database has rather than
the lowest common denominator. A SQL storage plugin publishes its store with `set_sql_store`,
and your plugin reads it back with `get_sql_store(app)`. List your plugin *after* the storage
plugin, so the store already exists when your `plug()` runs. That store offers:

- `dialect` — a `SqlDialect` to `match` on (no default case, so a new backend is a type error
  in your plugin rather than a silent fallback);
- `metadata` — declare your tables on it;
- `tables` — dltrack's own tables, by model, to reference from yours;
- `create_tables(*tables)` — create them at startup, the way dltrack's own are (idempotent,
  missing columns and indexes added, serialized across worker processes on Postgres);
- `engine` — for your own queries.

Here's a sketch of an embeddings plugin: pgvector with an HNSW index on Postgres, a JSON column
with brute-force search on sqlite. The `ON DELETE CASCADE` onto `Run` means purging a run removes
its embeddings too.

```python
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from dltrack import models
from dltrack.serve import SqlDialect, get_sql_store


def plug(app: Dash) -> None:
    store = get_sql_store(app)
    match store.dialect:
        case SqlDialect.POSTGRES:
            vector_type = Vector(768)  # needs POSTGRES_EXTENSIONS='["vector"]' (or a DBA to create it)
        case SqlDialect.SQLITE:
            vector_type = sa.JSON()
    embeddings = sa.Table(
        "RunEmbedding",
        store.metadata,
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("run_id", sa.BigInteger, sa.ForeignKey(store.tables[models.Run].c.id, ondelete="CASCADE")),
        sa.Column("embedding", vector_type, nullable=False),
    )
    if store.dialect is SqlDialect.POSTGRES:
        sa.Index(
            "idx_run_embedding_hnsw",
            embeddings.c.embedding,
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )
    store.create_tables(embeddings)
```

On Postgres, a nearest-neighbour query is then just
`sa.select(embeddings.c.run_id).order_by(embeddings.c.embedding.cosine_distance(query)).limit(10)`.
`dltrack/serve/_backend/tests/plugin_tables_test.py` does the same thing with a native array
column, on both backends.
