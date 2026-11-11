# Storage plugins

## What is this

Storage is split into two protocols, both in `dlboard/models/_data_store.py`:

- **`DataStore`** — structured metadata: users, projects, experiments, runs, metrics,
  hyperparameters, artifact *references*, soft-delete/restore/purge, audit log, kept runs and plugins' scoped
  settings (see [background-tasks.md](../background-tasks.md#plugin-settings-and-page-sections)).
- **`ArtifactStore`** — the artifact *blobs* themselves: `log_artifacts`, `link_artifacts`,
  `download_artifact`, `delete_artifact`.

They're separate on purpose — metadata and blob storage scale and fail independently, and a
deployment might reasonably want e.g. Postgres for metadata and S3 for blobs without those being
the same plugin.

## When you'd need this

You're deploying somewhere the built-in backends don't fit — e.g. a shared Postgres instead of
sqlite, or S3 (or any other S3-protocol store: MinIO, VAST, ...) instead of local disk for
artifacts. The built-ins are `dlboard/plugins/data_stores/sqlite.py`, `filesystem.py` (what
`LOCAL_DEPLOYMENT` uses), `postgres.py`, and `s3.py`, below.

## Postgres

`dlboard.plugins.POSTGRES_STORAGE` is `LOCAL_STORAGE` with a shared Postgres (`postgres`) in place of
`sqlite`. Install the driver with the `postgres` extra
(`pip install 'dlboard[postgres]'`), put `POSTGRES_STORAGE` in your own plugin list, and run it with
`dlboard serve custom --plugins yourmodule:PLUGINS`:

```python
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_STORAGE, themes

PLUGINS = [*POSTGRES_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Every setting is a `DLBOARD_POSTGRES_*` environment variable, with its default, in
[configuration.md](../configuration.md#postgres-storage). The code is `PostgresSettings` in
`dlboard/plugins/data_stores/postgres.py`.

The driver is libpq-based, so libpq's own environment (`PGPASSFILE`, `PGSERVICE`, ...) still works
underneath these. Each server worker process has its own pool, so keep
`workers × (pool_size + max_overflow)` under the server's `max_connections`. Workers starting at
once take turns creating/migrating the schema, under an advisory lock.

## S3

`dlboard.plugins.s3` is an `ArtifactStore` over any S3-protocol object store — AWS, MinIO, VAST,
or anything else that speaks the S3 API. Install boto3 with the `s3` extra
(`pip install 'dlboard[s3]'`), put it in your own plugin list alongside whichever metadata store
you're using (`dlboard.plugins.POSTGRES_S3_STORAGE` is `POSTGRES_STORAGE` with `s3` in place of
`filesystem`, ready-made), and run it with `dlboard serve custom --plugins yourmodule:PLUGINS`:

```python
from dlboard.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Every setting is a `DLBOARD_S3_*` environment variable, with its default, in
[configuration.md](../configuration.md#s3-artifact-storage). The code is `S3Settings` in
`dlboard/plugins/data_stores/s3.py`.

## Linking an already-uploaded artifact

A client doesn't have to upload through dlboard at all — `ArtifactStore.link_artifacts` registers
a blob a client already put somewhere this store can serve, with no bytes moved. The server only
accepts a ref inside the store's own `bucket`/`prefix` (or `filesystem`'s own root) or an
allowlisted read-only location (`DLBOARD_S3_EXTRA_READ_BUCKETS`); anything else is rejected outright, for
the whole batch. See [Client & logging](../client.md) for the client-side `Link` artifact kind that
exercises this.

A linked ref inside the store's own space is purged exactly like an uploaded one; a ref in an
allowlisted bucket is never deleted by dlboard, since dlboard didn't put it there.

## How do I build one

Implement the protocol (`DataStore` or `ArtifactStore`, or both) and register the instance from
`plug(app)`, with `set_data_store` and `set_artifact_store` from `dlboard.serve`:

```python
def plug(app: Dash) -> None:
    set_data_store(app, MyStore.get_or_create(...))
    # or, for an artifact store:
    set_artifact_store(app, MyArtifactStore(...))
```

Two things a `DataStore` read takes are hints a store may ignore. A `MetricReduction` (`fetch_metrics`) asks for
a row per run and key rather than every step, but a store that cannot answer that cheaply may return every step
instead: the reader reduces it itself. The `run_ids` of a read is not a hint: it limits which runs the rows are
of, and `None` means every run, which a caller passes explicitly.

You don't have to implement `DataStore` from scratch — `SQLStoreBase`
(`dlboard/serve/_backend/_sql_store_base.py`) already implements the full `DataStore` contract
(CRUD, cascading soft-delete, audit log, scopes) on SQLAlchemy Core. It generates every table from
its pydantic model and writes every query as a Core expression, so column types, identity columns,
quoting and bind style all come from SQLAlchemy's dialect for your database. A new SQL-backed store
only needs to subclass `SQLStoreBase`, pass `super().__init__()` an `sqlalchemy.Engine` (your
connection settings and pooling live there), and implement `_insert_ignoring_conflicts` — an
`INSERT ... ON CONFLICT DO NOTHING`, which each dialect spells through its own
`sqlalchemy.dialects.<name>.insert`. See `plugins/data_stores/sqlite.py` for the reference
implementation.

**Which databases.** The generic `SQLStoreBase` is tested on SQLite and Postgres, and nothing else. Another database
works in two ways. You can override the store's methods for it: a method you override is yours, and dlboard calls
it through the `DataStore` interface, so the generic SQL is not used there. Or you can write the SQL for your database
in your own plugin, which is then your own code to test. dlboard does not claim a database beyond the two it runs
against, and a plugin that writes SQL outside the `DataStore` interface is responsible for that SQL.

**Timestamps.** `_metric_history` reads each metric's `timestamp_utc` as the driver returns it, for speed, and
`MetricFrame.from_rows` accepts only an ISO-8601 string or an aware datetime. At startup dlboard reads one stored
metric timestamp and fails if the driver returned anything else. An empty experiment has nothing to read, so the
check passes until something has been logged, and a driver that returns the wrong type is only caught then.

For an `ArtifactStore` that's just somewhere to put and fetch whole blobs (as opposed to something
with its own, different storage model entirely), you don't have to implement the protocol from
scratch either — `plug_blob_store(app, backend, queue_size)` (`dlboard.plugins.data_stores`) sets a
`BlobArtifactStore` that already implements it on top of a much smaller `BlobBackend` protocol, and
starts what it needs in each process: `ref_for` (the ref a freshly written blob gets),
`access` (whether a ref is servable at all, and whether this backend owns it or merely allowlists
it — see "Linking" above), `exists`, `write`, `download`, and `delete`. `BlobArtifactStore` handles
staging an upload, batching writes off to a worker process, async ref-ingest back into the
`DataStore`, and the generic parts of `log_artifacts`/`link_artifacts`/`download_artifact`/
`delete_artifact` — a new backend only has to say how one blob is actually read, written, and
deleted. `filesystem.py`'s `FSBlobs` (local disk) and `s3.py`'s `S3Blobs` (any S3-protocol store)
are both just a `BlobBackend` plus a `pydantic_settings.BaseSettings` for configuration, read
inside `plug()` and handed to `plug_blob_store` — model a new one on whichever is closer to your backend.

Read from elsewhere in the app with `get_data_store()` / `get_artifact_store()` (`dlboard.serve`)
rather than passing the store instance around directly.
