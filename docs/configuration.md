# Configuration

## What is this

Every dlboard setting is an environment variable. This page lists all of them, grouped by what they
configure. A test (`dlboard/tests/configuration_docs_test.py`) fails if a setting exists in the code
but not here, so this list can't fall behind.

The command line only covers how to *run* the server (`--host`, `--port`, `--workers`, ...; see
`dlboard serve --help`). Everything about a deployment's behavior is an environment variable, which is
what a container or process manager passes anyway. `uv run --env-file .env dlboard serve ...` reads a
file of them; [`.env.example`](../.env.example) is a starting point.

Names are part of the [compatibility promise](compatibility.md): they don't change within a major
version line without a deprecation window.

## Server

| Variable | Default | |
|---|---|---|
| `DLBOARD_PLUGINS` | | Import path to the `list[PluginProtocol]` to serve, e.g. `myapp.deployment:PLUGINS`. Set by `dlboard serve`; the same as `serve custom --plugins`. |
| `DLBOARD_URL_PREFIX` | none | Serve under a path prefix (`dlboard` for `https://host/dlboard/`) when a [reverse proxy](reverse-proxy.md) forwards the full path unchanged. |
| `DLBOARD_TRUSTED_PROXIES` | `0` | How many reverse-proxy hops in front of dlboard to trust `X-Forwarded-*` from. Must be `1` or more behind a proxy, or HTTPS looks like HTTP and secure cookies break. |
| `DLBOARD_MAX_UPLOAD_MB` | `256` | Hard cap on any single request body, mainly artifact uploads. Over it, the request gets a 413. |
| `DLBOARD_POLL_INTERVAL_MS` | `30000` | How often an open experiment page checks for new data. |
| `DLBOARD_TICK_INTERVAL_MS` | `10000` | How often the "fetched Xs ago" label re-renders. Browser-only; never touches the server. |

## Production server (Granian)

These tune the process that `dlboard serve` runs under (not used by `--debug`).

| Variable | Default | |
|---|---|---|
| `DLBOARD_WORKERS_KILL_TIMEOUT_S` | `30` | How long a worker gets to exit after SIGTERM before it is killed. In-flight requests finish, and queued artifact blobs are written and recorded, inside this window. Keep it under your orchestrator's grace period (see [Shutting down](#shutting-down)). |
| `DLBOARD_WORKERS_MAX_RSS_MB` | `0` (off) | Gracefully restart a worker once its memory passes this many MB. A safety net against slow leaks. |
| `DLBOARD_BACKPRESSURE` | `0` (Granian decides) | Requests a worker accepts at once; more wait in the socket backlog. |
| `DLBOARD_HEADER_READ_TIMEOUT_S` | `30` | How long a client may take to send its request headers. Guards against slow-connection attacks. |
| `DLBOARD_KEEP_ALIVE` | `true` | Keep HTTP/1 connections open between requests. |

Worker and thread counts are `dlboard serve --workers` (default 2) and `--blocking-threads`
(default 16), not environment variables.

## Logging

| Variable | Default | |
|---|---|---|
| `DLBOARD_LOG_LEVEL` | `INFO` | A standard level name: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `DLBOARD_LOG_FORMAT` | `console` | `console` (human-readable) or `json` (one object per line, for a log collector). |
| `DLBOARD_ACCESS_LOG` | `false` | Log every request's method, path, status and duration. Off since a proxy usually does this already. |

## Authentication

Only matter with an identity-verifying provider such as `PASSWORD_AUTH`. See [auth.md](plugins/auth.md).

| Variable | Default | |
|---|---|---|
| `DLBOARD_SECRET_KEY` | | Signs session cookies. At least 32 characters (`openssl rand -hex 32`) and identical across every worker and replica. |
| `DLBOARD_SESSION_LIFETIME_HOURS` | `336` | How long a browser stays signed in. |
| `DLBOARD_SESSION_CLOCK_SKEW_SECONDS` | `120` | How far ahead of a replica's own clock a session cookie may be dated (by whichever replica last renewed it) and still be accepted. Replicas never agree to the second, so too little signs people out at random. Keep your nodes on NTP regardless. |
| `DLBOARD_SECURE_COOKIES` | `true` | Send the session cookie over HTTPS only. Turn off only for plain-HTTP local testing. |
| `DLBOARD_ADMIN_USERS` | none | Comma-separated usernames granted admin whenever they sign in. How the first admin is made. |
| `DLBOARD_ADMIN_GROUPS` | none | Comma-separated identity-provider groups whose members are admins. |
| `DLBOARD_NEW_PROJECT_ACCESS` | none (private) | The role (`viewer`, `editor`, `owner`) everyone gets on a newly created project. |
| `DLBOARD_PASSWORD_SIGNUP` | `admin_creates` | Who may create an account: `admin_creates`, `approval` (signs up disabled until an admin enables it) or `open`. |
| `DLBOARD_PASSWORD_MIN_LENGTH` | `12` | Minimum password length. |

## SQLite storage

`LOCAL_STORAGE`, used by `dlboard serve local`. See [storage.md](plugins/storage.md).

| Variable | Default | |
|---|---|---|
| `DLBOARD_SQLITE_LOCATION` | `~/.dlboard.sqlite` | The metadata database file. |
| `DLBOARD_SQLITE_BUSY_TIMEOUT_MS` | `30000` | How long SQLite retries a locked database before failing. |

## Filesystem artifact storage

| Variable | Default | |
|---|---|---|
| `DLBOARD_ARTIFACT_STORE_LOCATION` | `~/.dlboard_artifacts` | Where artifact blobs live. Uploads are staged in `.staging/` inside it, so give it enough room and keep it on one volume. |
| `DLBOARD_FILESYSTEM_STORE_QUEUE_SIZE` | `100` | Uploads that can wait for the writer. When full, new uploads wait up to 30 seconds, then get a 503 the client retries. |

## Postgres storage

`POSTGRES_STORAGE` and `POSTGRES_S3_STORAGE`. See [storage.md](plugins/storage.md).

| Variable | Default | |
|---|---|---|
| `DLBOARD_POSTGRES_HOST` | `localhost` | Host, IP or socket directory; a comma-separated list fails over between hosts. |
| `DLBOARD_POSTGRES_PORT` | `5432` | |
| `DLBOARD_POSTGRES_DATABASE` | `dlboard` | |
| `DLBOARD_POSTGRES_USER` | `dlboard` | |
| `DLBOARD_POSTGRES_PASSWORD` | none | Left unset, libpq falls back to `PGPASSFILE` / `~/.pgpass`. |
| `DLBOARD_POSTGRES_DB_SCHEMA` | `public` | Schema dlboard's tables live in; created if missing. |
| `DLBOARD_POSTGRES_APPLICATION_NAME` | `dlboard` | Shown in `pg_stat_activity`. |
| `DLBOARD_POSTGRES_SSLMODE` | `prefer` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`. Use `verify-full` in production. |
| `DLBOARD_POSTGRES_SSLROOTCERT` | none | CA certificate file. |
| `DLBOARD_POSTGRES_SSLCERT` | none | Client certificate file. |
| `DLBOARD_POSTGRES_SSLKEY` | none | Client key file. |
| `DLBOARD_POSTGRES_TARGET_SESSION_ATTRS` | none | E.g. `read-write` with several hosts, to always reach the primary. |
| `DLBOARD_POSTGRES_CONNECT_TIMEOUT_S` | `10` | |
| `DLBOARD_POSTGRES_STATEMENT_TIMEOUT_MS` | none | Server-side cap on any one statement. |
| `DLBOARD_POSTGRES_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS` | none | |
| `DLBOARD_POSTGRES_KEEPALIVES_IDLE_S` | none | Seconds idle before TCP keepalives start; for load balancers that drop idle connections. |
| `DLBOARD_POSTGRES_POOL_SIZE` | `5` | Connections per worker. Keep `workers × (pool_size + max_overflow)` under the server's `max_connections`. |
| `DLBOARD_POSTGRES_MAX_OVERFLOW` | `10` | |
| `DLBOARD_POSTGRES_POOL_TIMEOUT_S` | `30` | |
| `DLBOARD_POSTGRES_POOL_RECYCLE_S` | `1800` | Replace a pooled connection older than this. |
| `DLBOARD_POSTGRES_POOL_PRE_PING` | `true` | Check a connection is alive before using it. |
| `DLBOARD_POSTGRES_PREPARE_THRESHOLD` | `5` | Set to `null` behind PgBouncer's transaction pooling. |
| `DLBOARD_POSTGRES_CONNECT_ARGS` | `{}` | A JSON object of any other libpq parameter. |

## S3 artifact storage

`POSTGRES_S3_STORAGE`; needs the `s3` extra. See [storage.md](plugins/storage.md).

| Variable | Default | |
|---|---|---|
| `DLBOARD_S3_BUCKET` | required | |
| `DLBOARD_S3_PREFIX` | `dlboard` | Blobs live under `s3://bucket/prefix/...`. See [backup.md](backup.md) before changing it. |
| `DLBOARD_S3_ENDPOINT_URL` | AWS | Set for MinIO, VAST or any other S3-compatible store. |
| `DLBOARD_S3_REGION` | none | |
| `DLBOARD_S3_ACCESS_KEY_ID` | none | All credential settings unset: boto3's default chain (environment, profile, instance role). |
| `DLBOARD_S3_SECRET_ACCESS_KEY` | none | |
| `DLBOARD_S3_SESSION_TOKEN` | none | |
| `DLBOARD_S3_ADDRESSING_STYLE` | `auto` | `auto`, `path` or `virtual`. MinIO and most on-prem stores need `path`. |
| `DLBOARD_S3_VERIFY_TLS` | `true` | |
| `DLBOARD_S3_CA_BUNDLE` | none | CA bundle for a private-CA endpoint. |
| `DLBOARD_S3_DOWNLOAD_MODE` | `proxy` | `proxy` streams through dlboard; `presign` redirects the browser to a short-lived URL (the endpoint must be reachable from browsers). |
| `DLBOARD_S3_PRESIGN_TTL_S` | `300` | |
| `DLBOARD_S3_EXTRA_READ_BUCKETS` | none | Other buckets dlboard may link and read, never write or delete. |
| `DLBOARD_S3_QUEUE_SIZE` | `100` | Uploads that can wait for the writer, as `DLBOARD_FILESYSTEM_STORE_QUEUE_SIZE`. |

## Client (training scripts)

Read by `DLBoardLogger` and the REST client in the training process. See [client.md](client.md).

| Variable | Default | |
|---|---|---|
| `DLBOARD_API_KEY` | none | An API token (`dlb_...`) from the server's Account page. |
| `DLBOARD_RUN_ID` | none | Attach to an existing run instead of creating one. |
| `DLBOARD_USER` | OS login name | The username to attribute a run to on a server that doesn't verify identity. |
| `DLBOARD_CONNECT_TIMEOUT_S` | `10` | How long a request waits to connect. |
| `DLBOARD_READ_TIMEOUT_S` | `60` | How long a request waits for the server's response. |

## Shutting down

On SIGTERM (a deploy, `docker stop`, `systemctl stop`), each worker stops accepting requests, finishes
the ones in flight, writes every queued artifact blob and records its reference in the database, then
exits. All of that has to fit in `DLBOARD_WORKERS_KILL_TIMEOUT_S`; whatever doesn't is abandoned with a
warning, and the client's retry of that upload recovers it.

The orchestrator must wait at least that long before it sends SIGKILL. Docker's default is 10 seconds,
shorter than the default here, so set `docker stop --time` / `stop_grace_period` (the bundled
`docker-compose.yml` already does) or Kubernetes' `terminationGracePeriodSeconds` to about 5 seconds
more than `DLBOARD_WORKERS_KILL_TIMEOUT_S`. A server whose artifact writer has died answers uploads with
`503` and `Retry-After`, which the client treats as temporary.
