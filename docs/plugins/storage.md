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

You're deploying somewhere the built-in backends don't fit — a shared Postgres instance instead of
per-deployment sqlite, or S3/GCS instead of local filesystem for artifacts. The built-ins,
`dltrack/plugins/data_stores/sqlite.py` and `filesystem.py`, are what `LOCAL_DEPLOYMENT` uses.

## How do I build one

Implement the protocol (`DataStore` or `ArtifactStore`, or both) and register the instance from
`plug(app)`:

```python
def plug(app: Dash) -> None:
    set_data_store(app, MyStore.get_or_create(...))       # dltrack.serve._backend._data_store
    # or, for a blob store:
    set_artifact_store(app, MyArtifactStore.get_or_create(...))
```

You don't have to implement `DataStore` from scratch against raw SQL — `SQLStoreBase`
(`dltrack/serve/_backend/_sql_store_base.py`) already implements the full `DataStore` contract
(CRUD, cascading soft-delete, audit log, scopes) against an abstract `_execute_raw_sql`/
`_execute_raw_sql_query_many`. A new SQL-backed store (Postgres, MySQL) usually only needs to
subclass `SQLStoreBase` and supply those two methods plus connection handling — see
`plugins/data_stores/sqlite.py` for the ~30-line reference implementation, including how it runs
`dltrack/serve/_backend/_migrations.py`'s schema migrations on startup.

An `ArtifactStore` has no equivalent base class — `plugins/data_stores/filesystem.py` is the
reference implementation (local disk, `protocol = "file"`) to model a new one on. Configuration
(bucket name, credentials, root path) should come from `pydantic_settings.BaseSettings`, read
inside `plug()` — see either built-in for the pattern.

Read from elsewhere in the app with `get_data_store()` / `get_artifact_store()`
(`dltrack/serve/_backend/_data_store.py`) rather than passing the store instance around directly.
