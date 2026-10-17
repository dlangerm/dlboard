# Backup and restore

## What is this

A dlboard deployment's data lives in two places: the **metadata database** (projects, experiments, runs,
metrics, views, users) and the **artifact blobs** (images and other files). Neither alone is a backup.
The database holds a reference to each blob, so a database without its blobs shows broken images, and
blobs without the database are unnamed files.

## Where your data is

| Deployment | Metadata | Artifacts |
|---|---|---|
| `dlboard serve local` | `DLBOARD_SQLITE_LOCATION` (`~/.dlboard.sqlite`) | `DLBOARD_ARTIFACT_STORE_LOCATION` (`~/.dlboard_artifacts`) |
| Postgres + filesystem | the Postgres database | `DLBOARD_ARTIFACT_STORE_LOCATION` volume |
| Postgres + S3 | the Postgres database | the S3 bucket and prefix |

## SQLite

Don't copy the file while the server runs. A running server may have recent changes in a `-wal` file
next to it, and a copy taken mid-write can be corrupt. Use SQLite's own backup, which is safe on a live
database:

```bash
sqlite3 ~/.dlboard.sqlite ".backup '/backups/dlboard-$(date +%F).sqlite'"
```

To restore, stop the server, put the backup at `DLBOARD_SQLITE_LOCATION` (delete any `-wal` and `-shm`
files beside the old one), and start it.

## Postgres

```bash
pg_dump --format=custom --file=dlboard-$(date +%F).dump "$DATABASE_URL"
pg_restore --clean --if-exists --dbname="$DATABASE_URL" dlboard-2026-10-07.dump   # restore
```

If you set `DLBOARD_POSTGRES_DB_SCHEMA`, add `--schema=<name>` to the dump. Managed Postgres services
(RDS, Cloud SQL, ...) have point-in-time recovery; turn it on and it covers this.

## Filesystem artifacts

Snapshot the volume that holds `DLBOARD_ARTIFACT_STORE_LOCATION` (LVM, ZFS, EBS, your storage array),
or `rsync -a` it. Blobs are written once and never changed, so a plain copy of a running store is
consistent; the one thing to skip is `.staging/`, which holds uploads still in flight.

Take the **database backup first, then the artifacts**. An artifact copied after the database will have
at least every blob the database refers to. The other order can leave references to blobs your copy
lacks.

## S3 artifacts

- Turn on **bucket versioning** (and a lifecycle rule for old versions), or replicate the bucket to
  another region or account. Purging an artifact in dlboard permanently deletes its object.
- dlboard stores blobs under `s3://<bucket>/<prefix>/...`, and the database holds the full
  `s3://` refs. **Restore into the same bucket and prefix**, or the refs point nowhere. Changing
  `DLBOARD_S3_BUCKET` or `DLBOARD_S3_PREFIX` on a deployment that has data is the same as losing its
  artifacts.
- Artifacts linked from `DLBOARD_S3_EXTRA_READ_BUCKETS` are not dlboard's to back up. They live in
  buckets you own elsewhere.

## Checking a backup

A backup you've never restored is a hope. Periodically restore into a scratch copy: start
`dlboard serve custom` against it with a different port and `DLBOARD_ARTIFACT_STORE_LOCATION`, and open
a recent experiment with an image chart.
