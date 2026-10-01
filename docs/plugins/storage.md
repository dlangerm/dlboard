# Storage plugins

## What is this

Storage is split into two protocols, both in `dltrack/models/_data_store.py`:

- **`DataStore`** — structured metadata: users, projects, experiments, runs, metrics,
  hyperparameters, artifact *references*, soft-delete/restore/purge, audit log.
- **`ArtifactStore`** — the artifact *blobs* themselves: `log_artifacts`, `link_artifacts`,
  `download_artifact`, `delete_artifact`.

They're separate on purpose — metadata and blob storage scale and fail independently, and a
deployment might reasonably want e.g. Postgres for metadata and S3 for blobs without those being
the same plugin.

## When you'd need this

You're deploying somewhere the built-in backends don't fit — e.g. a shared Postgres instead of
sqlite, or S3 (or any other S3-protocol store: MinIO, VAST, ...) instead of local disk for
artifacts. The built-ins are `dltrack/plugins/data_stores/sqlite.py`, `filesystem.py` (what
`LOCAL_DEPLOYMENT` uses), `postgres.py`, and `s3.py`, below.

## Postgres

`dltrack.plugins.POSTGRES_STORAGE` (`postgres` + `filesystem` + the artifact purge worker) swaps
sqlite for a shared Postgres. Install the driver with the `postgres` extra
(`pip install 'dltrack[postgres]'`), put `POSTGRES_STORAGE` in your own plugin list, and run it with
`dltrack serve custom --plugins yourmodule:PLUGINS`:

```python
from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_STORAGE, themes

PLUGINS = [*POSTGRES_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
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
| Anything else | `POSTGRES_CONNECT_ARGS` — a JSON object of extra libpq parameters, e.g. `{"sslcrl": "..."}` |

The driver is libpq-based, so libpq's own environment (`PGPASSFILE`, `PGSERVICE`, ...) still works
underneath these. Each server worker process has its own pool, so keep
`workers × (pool_size + max_overflow)` under the server's `max_connections`. Workers starting at
once take turns creating/migrating the schema, under an advisory lock.

## S3

`dltrack.plugins.s3` is an `ArtifactStore` over any S3-protocol object store — AWS, MinIO, VAST,
or anything else that speaks the S3 API. Install boto3 with the `s3` extra
(`pip install 'dltrack[s3]'`), put it in your own plugin list alongside whichever metadata store
you're using (`dltrack.plugins.POSTGRES_S3_STORAGE` is `postgres` + `s3` + the artifact purge
worker, ready-made), and run it with `dltrack serve custom --plugins yourmodule:PLUGINS`:

```python
from dltrack.plugins import BUILTIN_BACKEND, BUILTIN_CHARTS, PASSWORD_AUTH, POSTGRES_S3_STORAGE, themes

PLUGINS = [*POSTGRES_S3_STORAGE, *PASSWORD_AUTH, *BUILTIN_BACKEND, *BUILTIN_CHARTS, themes.default]
```

Every setting is an `S3_*` environment variable. The full list, with docs, is `S3Settings` in
`dltrack/plugins/data_stores/s3.py`:

| Concern | Variables |
|---|---|
| Where blobs live | `S3_BUCKET` (required), `_PREFIX` (default `dltrack`; every blob lives under `s3://bucket/prefix/...`) |
| Connection | `S3_ENDPOINT_URL` (unset talks to AWS; set it to any other S3-protocol endpoint), `_REGION`, `_ADDRESSING_STYLE` (`path` for MinIO/most on-prem/VAST — they don't support virtual-hosted bucket addressing) |
| Credentials | `S3_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY`, `_SESSION_TOKEN` — left unset, boto3 falls back to its own default chain (environment, shared config/profile, instance/container role) |
| TLS | `S3_VERIFY_TLS` (default `true`), `_CA_BUNDLE` (a CA bundle for a self-signed/private-CA endpoint) |
| Downloads | `S3_DOWNLOAD_MODE` (`proxy`, the default — streams through this server, reachable even from a private endpoint; or `presign` — a 302 to a short-lived presigned URL, no bytes through this server, but the endpoint must be reachable from the browser), `_PRESIGN_TTL_S` |
| Linking (see below) | `S3_EXTRA_READ_BUCKETS` — a JSON array of other buckets this store's credentials may read (and link/download) from, but never write to or delete from |
| Throughput | `S3_QUEUE_SIZE` |

## Linking an already-uploaded artifact

A client doesn't have to upload through dltrack at all — `ArtifactStore.link_artifacts` registers
a blob a client already put somewhere this store can serve, with no bytes moved. The server only
accepts a ref inside the store's own `bucket`/`prefix` (or `filesystem`'s own root) or an
allowlisted read-only location (`S3_EXTRA_READ_BUCKETS`); anything else is rejected outright, for
the whole batch. See [Client & logging](../client.md) for the client-side `Link` artifact kind that
exercises this.

A linked ref inside the store's own space is purged exactly like an uploaded one; a ref in an
allowlisted bucket is never deleted by dltrack, since dltrack didn't put it there.

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
`sqlalchemy.dialects.<name>.insert`. See `plugins/data_stores/sqlite.py` for the reference
implementation.

For an `ArtifactStore` that's just somewhere to put and fetch whole blobs (as opposed to something
with its own, different storage model entirely), you don't have to implement the protocol from
scratch either — `BlobArtifactStore` (`plugins/data_stores/_blob_store.py`) already implements it
on top of a much smaller `BlobBackend` protocol: `ref_for` (the ref a freshly written blob gets),
`access` (whether a ref is servable at all, and whether this backend owns it or merely allowlists
it — see "Linking" above), `exists`, `write`, `download`, and `delete`. `BlobArtifactStore` handles
staging an upload, batching writes off to a worker process, async ref-ingest back into the
`DataStore`, and the generic parts of `log_artifacts`/`link_artifacts`/`download_artifact`/
`delete_artifact` — a new backend only has to say how one blob is actually read, written, and
deleted. `filesystem.py`'s `FSBlobs` (local disk) and `s3.py`'s `S3Blobs` (any S3-protocol store)
are both just a `BlobBackend` plus a `pydantic_settings.BaseSettings` for configuration, read
inside `plug()` — model a new one on whichever is closer to your backend.

Read from elsewhere in the app with `get_data_store()` / `get_artifact_store()`
(`dltrack/serve/_backend/_data_store.py`) rather than passing the store instance around directly.
