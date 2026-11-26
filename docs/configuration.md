# Configuration

## What is this

Every dlboard setting is an environment variable.
This page lists all of them, grouped by what they configure.
A test (`dlboard/tests/configuration_docs_test.py`) fails if a setting exists in the code but not here, so this list can't fall behind.

The command line only covers how to *run* the server (`--host`, `--port`, `--workers`, ...; see `dlboard serve --help`).
Everything about a deployment's behavior is an environment variable, which is what a container or process manager passes anyway.
`uv run --env-file .env dlboard serve ...` reads a file of them; [`.env.example`](../.env.example) is a starting point.

Names are part of the [compatibility promise](compatibility.md): they don't change within a major version line without a deprecation window.

## Server

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_PLUGINS` | | Import path to the `list[PluginProtocol]` to serve, e.g. `myapp.deployment:PLUGINS`. Set by `dlboard serve`; the same as `serve custom --plugins`. |
| `DLBOARD_URL_PREFIX` | none | Serve under a path prefix (`dlboard` for `https://host/dlboard/`) when a [reverse proxy](reverse-proxy.md) forwards the full path unchanged. |
| `DLBOARD_TRUSTED_PROXIES` | `0` | How many reverse-proxy hops in front of dlboard to trust `X-Forwarded-*` from. Must be `1` or more behind a proxy, or HTTPS looks like HTTP and secure cookies break. |
| `DLBOARD_COMPRESS_RESPONSES` | `true` | Compress what the server sends (pages, scripts, chart data) as zstd, Brotli or gzip, whichever the browser takes: a page of charts is about a fifth the size. Never a picture or other already-compressed artifact, and never to a browser on the same machine, where it would only cost time. Turn it off when a [reverse proxy](reverse-proxy.md) in front already compresses. |
| `DLBOARD_MAX_UPLOAD_MB` | `256` | Hard cap on any single request body, mainly artifact uploads. Over it, the request gets a 413. |
| `DLBOARD_MAX_HISTOGRAM_BUCKETS` | `1024` | The most buckets one logged histogram may have. Over it, the server rejects the batch; a client checks its own histograms against the same setting in its own environment, so one that bins finer needs it raised there too. |
| `DLBOARD_MAX_HISTOGRAM_STEPS` | `256` | Steps per run a panel reads of each histogram it shows, however many were logged: a longer run is thinned to every 2ⁿ-th step, so the steps shown stay put as it trains. Raise it for a finer picture of a long run, lower it to make a panel of many histograms cheaper. |
| `DLBOARD_WIDE_HISTORY_METRICS` | `200` | A chart that draws every step of more metrics than this (a table of a row per step over a pattern, say) shows a badge saying how many. |
| `DLBOARD_MAX_HISTORY_METRICS` | `5000` | A chart that would draw every step of more metrics than this is not drawn, and their history is not read: it says to show their last value or their spread instead. |
| `DLBOARD_MAX_TABLE_ROWS` | `5000` | A table chart with a row per value of a field (a row per step, say) that would have more rows than this is not drawn: it says how many, and to show a row per run or a coarser field (an epoch) instead. Every row goes to the browser, so a row per step of long runs is megabytes. |
| `DLBOARD_DEFAULT_SHOWN_RUNS` | `20` | How many of an experiment's newest runs its charts draw until a view selects runs of its own with the eye in the run list, which lists every run. Raise it to draw more by default, at the cost of reading that many runs for every chart on every page load and live update. |
| `DLBOARD_MAX_SHOWN_RUNS` | `100` | The most runs a view may draw (with the eye in the run list). A selection past this is refused, with a notice, so no view can read every run of an experiment of thousands. Raise it to compare more runs at once, at the cost of reading that many runs for every chart on every page load and live update. |
| `DLBOARD_MAX_COMPARISON_RUNS` | `100` | The most runs a comparison holds, however many it is given: adding runs to one that would then hold more is refused, with how many fit. Each run's metrics are read for every chart on the comparison page, so this bounds what it reads. Kept apart from `DLBOARD_MAX_SHOWN_RUNS` because a comparison is picked by hand and kept, and may reasonably hold more than an ad-hoc selection of runs to draw. |
| `DLBOARD_HISTOGRAM_ACCENT_COLOR` | `#3b82f6` | The one color (`#rrggbb`) a histogram chart draws its bands and bars in; a Plotly figure cannot use the theme's CSS variables. |
| `DLBOARD_HISTOGRAM_ROW_GAP_PX` | `52` | Space between one run's row and the next in a histogram chart, with room for the run's name and the x axis. |
| `DLBOARD_HISTOGRAM_MAX_TICKS` | `6` | How many buckets a histogram chart's value axis names (by lower edge). |
| `DLBOARD_HISTOGRAM_ASPECT` | `2.5` | How much wider than tall a histogram chart is by default. |
| `DLBOARD_PLOTLY_REMOTE_RESOURCES` | `false` | Let a logged Plotly figure load what it names from other servers: its pictures (`layout.images`, an image trace's `source`), a GeoJSON by URL, map styles and tiles, a `geo` map's outlines. Off, every page's Content-Security-Policy lets it load pictures, media, fonts and data from dlboard alone, so the browser refuses them, since anyone who can log to a project could otherwise make the browser of everyone who opens its charts request a URL of their choosing (which says that they looked, and from where). On, the policy no longer restricts what a page loads. A picture given as a `data:` URI is always drawn. |
| `DLBOARD_PLOTLY_MAP_TILES` | unset | While `DLBOARD_PLOTLY_REMOTE_RESOURCES` is off, the raster tiles a Plotly map draws its map from, as a URL template (`https://tiles.example.com/{z}/{x}/{y}.png`), a tile server the deployment chooses; pages may load from its origin. Unset, a map is drawn on a blank background. |
| `DLBOARD_POLL_INTERVAL_MS` | `30000` | How often an open experiment page checks for new data. |
| `DLBOARD_TICK_INTERVAL_MS` | `10000` | How often the "fetched Xs ago" label re-renders. Browser-only; never touches the server. |

## Production server (Granian)

These tune the process that `dlboard serve` runs under (not used by `--debug`).

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_WORKERS_KILL_TIMEOUT_S` | `30` | How long a worker gets to exit after SIGTERM before it is killed. In-flight requests finish, and queued artifact blobs are written and recorded, inside this window. Keep it under your orchestrator's grace period (see [Shutting down](#shutting-down)). |
| `DLBOARD_WORKERS_MAX_RSS_MB` | `0` (off) | Gracefully restart a worker once its memory passes this many MB. A safety net against slow leaks. |
| `DLBOARD_BACKPRESSURE` | `0` (Granian decides) | Requests a worker accepts at once; more wait in the socket backlog. |
| `DLBOARD_HEADER_READ_TIMEOUT_S` | `30` | How long a client may take to send its request headers. Guards against slow-connection attacks. |
| `DLBOARD_KEEP_ALIVE` | `true` | Keep HTTP/1 connections open between requests. |
| `DLBOARD_SSL_CERTIFICATE` | none | Serve HTTPS from this certificate (with its chain, PEM), with no proxy in front. Needs `DLBOARD_SSL_KEYFILE`. See [Without a proxy](reverse-proxy.md#without-a-proxy-native-tls). |
| `DLBOARD_SSL_KEYFILE` | none | The certificate's key (PEM, unencrypted). Replace both files and send the server `SIGHUP` to renew. |
| `DLBOARD_STARTUP_WAIT_S` | `120` | How long to keep retrying a database or object store that cannot be reached yet before the server gives up starting. `0` fails on the first error. A wrong password or a missing bucket always fails at once. See [After a reboot](docker.md#after-a-reboot). |

Worker and thread counts are `dlboard serve --workers` (default 2) and `--blocking-threads` (default 16), not environment variables.

## Background tasks

The server starts one custodian process beside its workers, which runs every background task its plugins register, and restarts it if it dies or hangs.
Every server sharing a database starts one, and they share the work through the database.

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_CUSTODIAN_ENABLED` | `true` | Whether the server starts a custodian at all. Off, no background task runs on this server (another sharing the database may still run them). |
| `DLBOARD_CUSTODIAN_TICK_S` | `1.0` | How often the custodian checks on its tasks and on the server that started it, in memory, with no database query. |
| `DLBOARD_CUSTODIAN_POLL_S` | `5.0` | How often it reads the database for runs asked for. |
| `DLBOARD_CUSTODIAN_BEAT_S` | `15.0` | How often it records in the database that it is alive. A task it runs one at a time is held for three beats past the last: a custodian that dies holds its tasks only that long. |
| `DLBOARD_CUSTODIAN_STALL_S` | `30.0` | How long the server waits for the custodian's heartbeat before it takes it for hung, kills it, and starts another. |
| `DLBOARD_CUSTODIAN_STARTUP_S` | `600.0` | How long a custodian may take to start (build the app, and migrate the database if it is first) before the server takes it for hung. |
| `DLBOARD_CUSTODIAN_DUTY_CYCLE` | `0.25` | The share of time a task may spend in a batch of work: after one that took `t`, it waits `t * (1 - d) / d`, so it takes at most this share of the database's time, and less when the database is busy. |
| `DLBOARD_CUSTODIAN_CLAIM_S` | `300` | How long a claim on a work item lasts: an item whose custodian died is claimed again after this. |
| `DLBOARD_TRASH_RETENTION_DAYS` | `30` | How long anything stays in the Trash, restorable, before it is purged for good, whoever deleted it. Counted from no earlier than the first time the server checks, so what was in the Trash before an upgrade is not purged the moment it lands. |
| `DLBOARD_LOST_RUN_AFTER_S` | `600` | How long a running run may go unheard from -- no batch, no heartbeat -- before it is given up as lost: *unknown*, ended when it was last heard from. At least 180. Heard from again, it runs again. See [client.md](client.md). |

## Webhooks

Posting each run that ends to its project's webhooks: see [webhooks.md](webhooks.md).

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_WEBHOOK_ALLOWED_HOSTS` | none | Comma-separated hosts a webhook may post to though they resolve to an address that isn't public (this machine, a private network, a tailnet): an internal chat server, say. |
| `DLBOARD_WEBHOOK_PUBLIC_URL` | none | Where people reach the server (`https://dlboard.example.com`), for the links in what a webhook posts; unset, each webhook's links use the address its owner reached the server at. |
| `DLBOARD_WEBHOOK_MAX_PER_PROJECT` | `10` | The most webhooks a project has. |
| `DLBOARD_WEBHOOK_TIMEOUT_S` | `5` | How long a post waits to connect, and then for the answer. |
| `DLBOARD_WEBHOOK_ATTEMPTS` | `10` | Tries a post gets, a minute apart and doubling, before its webhook is turned off: about eight and a half hours. |
| `DLBOARD_WEBHOOK_INTERVAL_S` | `30` | How often the server looks for runs that ended. |

## Retention

The retention plugin deletes a project's oldest runs (to the Trash) once they pass its rule: the newest N runs, or the runs of the last N days.
A kept run is never deleted by it.
These set what a new project starts with, how far an owner may go, and how a changed rule takes effect; the rule itself is set per project, on its page.

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_RETENTION_DEFAULT_MAX_RUNS` | none | A new project keeps this many runs of each `DLBOARD_RETENTION_DEFAULT_UNIT`, and deletes older ones. Unset, with no default age either, a new project keeps every run (or, with a cap set, is held to the first cap). A project with no rule of its own (a new one, or one from before retention) gets this default the next time retention looks for projects (`DLBOARD_RETENTION_INTERVAL_S`), after the grace period like any change. |
| `DLBOARD_RETENTION_DEFAULT_UNIT` | `experiment` | What `DLBOARD_RETENTION_DEFAULT_MAX_RUNS` counts over: each `experiment`'s runs on their own, or every run of the `project` together. |
| `DLBOARD_RETENTION_DEFAULT_MAX_AGE_DAYS` | none | A new project keeps runs created in the last this many days, and deletes older ones. Not with `DLBOARD_RETENTION_DEFAULT_MAX_RUNS`. |
| `DLBOARD_RETENTION_MAX_RUNS` | none | The most runs a project's owner may have each experiment keep; only an admin sets a project past it. With any cap set, an owner can no longer keep every run, nor choose a kind of rule that has no cap. |
| `DLBOARD_RETENTION_MAX_AGE_DAYS` | none | The oldest, in days, a project's owner may have runs kept; only an admin sets a project past it. |
| `DLBOARD_RETENTION_GRACE_DAYS` | `7` | How long a changed rule waits before it takes effect. Until then the rule before it is still enforced, so changing it again never puts retention off. |
| `DLBOARD_RETENTION_RUN_SLACK` | `10` | How many runs past its count a project or experiment may hold before runs are deleted, down to the count: runs are deleted in batches, not one for each new run. |
| `DLBOARD_RETENTION_AGE_SLACK_DAYS` | `1` | How many days past its age a run may get before runs are deleted, down to the age. |
| `DLBOARD_RETENTION_RUNNING_GRACE_DAYS` | `7` | How long a run that is still running is never deleted for. One running longer than this has lost its client, which then never reported that it ended, and is deleted as any other. |
| `DLBOARD_RETENTION_WARN_AFTER_HOURS` | `24` | How long a project may hold runs its rules say to delete before its page says it is out of spec: retention has not caught up with it in that time. |
| `DLBOARD_RETENTION_LOCK_AFTER_HOURS` | none | How long a project may hold runs its rules say to delete before it takes no new runs: the REST API then refuses them (HTTP 423) until retention has caught up, so a project is not filled faster than it is drained. Runs already logging are never refused. Unset, a project is only warned. At least `DLBOARD_RETENTION_WARN_AFTER_HOURS`. |
| `DLBOARD_RETENTION_INTERVAL_S` | `300` | How often retention looks for projects to check. One read gives every project's rule; a project with none is not looked at again (unless a default or cap gives it one), so idle projects cost the server nothing and a project costs a statement only when its rule changes. |
| `DLBOARD_RETENTION_BATCH` | `100` | How many runs one batch deletes, in one transaction. The custodian paces itself between batches (`DLBOARD_CUSTODIAN_DUTY_CYCLE`). |
| `DLBOARD_RETENTION_PROJECT_BUDGET` | `5000` | The most runs one check of a project deletes before it moves on, so one project far past its rule cannot starve the others. It is caught up over several checks, and shows as out of spec until it is. |

## Logging

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_LOG_LEVEL` | `INFO` | A standard level name: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Governs dlboard's own logs and Granian's; any other library that logs speaks only from `WARNING`. |
| `DLBOARD_LOG_FORMAT` | `console` | `console` (human-readable) or `json` (one object per line, for a log collector). Applies to everything the server prints, Granian's own lines and Flask's errors included. |
| `DLBOARD_ACCESS_LOG` | `false` | Log every request's method, path, status and duration. Off since a proxy usually does this already. |

## Telemetry

Off unless you set one of these; a deployment that does not never loads the OpenTelemetry SDK.
Needs the `otel` extra (`pip install 'dlboard[otel]'`; the Docker image has it).
The exporters also read the standard `OTEL_*` variables (endpoint, headers, service name, resource attributes, export interval).
See [telemetry.md](telemetry.md) for the metrics and how to point it at Prometheus, Mimir or Loki.

| Variable | Default | |
| --- | --- | --- |
| `OTEL_SERVICE_NAME` | `dlboard` | The standard OpenTelemetry variable: the `service.name` everything is reported under. |
| `DLBOARD_TELEMETRY_METRICS` | none | Comma-separated exporters to send metrics to: `prometheus` (scrape `/metrics`), `otlp` (push to a collector), or both. |
| `DLBOARD_TELEMETRY_LOGS` | `false` | Also send every log event over OTLP. Independent of `DLBOARD_LOG_FORMAT`, which still controls what goes to stdout. |
| `DLBOARD_TELEMETRY_TRACES` | `false` | Send distributed traces over OTLP, from the server and from the client's shipping processes. Needs the `otel` extra (`dlboard[otel]` on the server, `dlboard-client[otel]` in a training environment). The exporter reads the standard `OTEL_EXPORTER_OTLP_*` variables. |
| `OTEL_TRACES_EXPORTER` | `otlp` | With `DLBOARD_TELEMETRY_TRACES`, comma-separated: where spans go. `otlp` (HTTP/protobuf), `console`, or `none` (nothing exported, though traces still cross processes). Others are a startup error. The standard OpenTelemetry variable. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | The OTLP wire protocol of every signal (traces, metrics, logs): `http/protobuf`, or `grpc` with the `otel-grpc` extra (`dlboard-client[otel-grpc]`, `dlboard[otel-grpc]`); `grpc` without it, or `http/json`, stops startup. The standard OpenTelemetry variable. |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | `http/protobuf` | As above, for traces alone; overrides `OTEL_EXPORTER_OTLP_PROTOCOL`. |
| `OTEL_EXPORTER_OTLP_METRICS_PROTOCOL` | `http/protobuf` | As above, for metrics alone (`DLBOARD_TELEMETRY_METRICS=otlp`). |
| `OTEL_EXPORTER_OTLP_LOGS_PROTOCOL` | `http/protobuf` | As above, for logs alone (`DLBOARD_TELEMETRY_LOGS`). |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | `http` | With `DLBOARD_TELEMETRY_TRACES`, which generation of attribute names the server's instrumentation writes. dlboard sets `http` (`http.request.method`, ...) to match the client's spans, unless you set it. The standard OpenTelemetry variable. |
| `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS` | `Authorization,Cookie,Set-Cookie` | With `DLBOARD_TELEMETRY_TRACES`, header names (regular expressions) whose values are redacted from a span, if you record headers at all (with OpenTelemetry's own capture-headers variables, which dlboard never sets). The standard OpenTelemetry Python variable. |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | none | With `DLBOARD_TELEMETRY_TRACES`, comma-separated instrumentation names (`sqlalchemy`, `psycopg`, `botocore`, ...) the server should not trace. The standard OpenTelemetry Python variable. |
| `OTEL_PYTHON_EXCLUDED_URLS` | none | With `DLBOARD_TELEMETRY_TRACES`, comma-separated regular expressions of request URLs not to trace, besides dlboard's own (health probes, `/metrics`, static files). The standard OpenTelemetry Python variable. |
| `OTEL_PYTHON_FLASK_EXCLUDED_URLS` | none | As above, for Flask alone; when set it replaces `OTEL_PYTHON_EXCLUDED_URLS`, as OpenTelemetry's own instrumentation reads the two. |
| `DLBOARD_TELEMETRY_PROMETHEUS_DIR` | system temp dir | Where `/metrics` keeps each process's snapshot. `dlboard serve` and `run_production_server` make a fresh directory inside it per run and remove it on exit; set it to put that on a tmpfs, say. If you start the workers some other way, point them all at one directory here (it is used as it is, and not cleaned up). |
| `DLBOARD_TELEMETRY_PROMETHEUS_SNAPSHOT_S` | `15` | How often each process writes its snapshot, so how stale another worker's numbers on `/metrics` can be. |

## Authentication

Only matter with an identity-verifying provider such as `PASSWORD_AUTH`.
See [auth.md](plugins/auth.md).

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_SECRET_KEY` | | Signs session cookies. At least 32 characters (`openssl rand -hex 32`) and identical across every worker and replica. |
| `DLBOARD_SESSION_LIFETIME_HOURS` | `336` | How long a browser stays signed in. |
| `DLBOARD_RUN_TOKEN_LIFETIME_HOURS` | `168` | How long a [run token](client.md#how-it-works) (`dlboard run-token`) logs to its run before it stops working. |
| `DLBOARD_SESSION_CLOCK_SKEW_SECONDS` | `120` | How far ahead of a replica's own clock a session cookie may be dated (by whichever replica last renewed it) and still be accepted. Replicas never agree to the second, so too little signs people out at random. Keep your nodes on NTP regardless. |
| `DLBOARD_SECURE_COOKIES` | `true` | Send the session cookie over HTTPS only. Turn off only for plain-HTTP local testing. |
| `DLBOARD_ADMIN_USERS` | none | Comma-separated usernames granted admin whenever they sign in. How the first admin is made. |
| `DLBOARD_ADMIN_GROUPS` | none | Comma-separated identity-provider groups whose members are admins. |
| `DLBOARD_TRUSTED_HEADER_ISSUER` | required by `TRUSTED_HEADER_AUTH` | A stable name for the identity source behind the proxy (`sso.example.com`). A user is known by it and the user header together. |
| `DLBOARD_TRUSTED_HEADER_PROXIES` | required by `TRUSTED_HEADER_AUTH` | Comma-separated addresses or networks (`10.0.0.5`, `172.18.0.0/16`) of the proxy: the only peers whose identity headers are believed, by the connection's own address. |
| `DLBOARD_TRUSTED_HEADER_USER` | `X-Forwarded-User` | The header with the caller's stable id, which is also their username. |
| `DLBOARD_TRUSTED_HEADER_EMAIL` | `X-Forwarded-Email` | The header with their email. |
| `DLBOARD_TRUSTED_HEADER_GROUPS` | `X-Forwarded-Groups` | The header with their comma-separated groups, matched against group grants and `DLBOARD_ADMIN_GROUPS`. |
| `DLBOARD_NEW_PROJECT_ACCESS` | none (private) | The role (`viewer`, `editor`, `owner`) everyone gets on a newly created project. |
| `DLBOARD_PASSWORD_SIGNUP` | `admin_creates` | Who may create an account: `admin_creates`, `approval` (signs up disabled until an admin enables it) or `open`. |
| `DLBOARD_PASSWORD_MIN_LENGTH` | `12` | Minimum password length. |
| `DLBOARD_PASSWORD_FIRST_ADMIN_SETUP` | `false` | Serve `/setup`, which creates the first admin on an empty database using a token from the server log. Turn it on for first setup, then off again: startup fails if it's on and the database already has users. |

## SQLite storage

`LOCAL_STORAGE`, used by `dlboard serve local`.
See [storage.md](plugins/storage.md).

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_SQLITE_LOCATION` | `.dlboard/db.sqlite` | The metadata database file. |
| `DLBOARD_SQLITE_BUSY_TIMEOUT_MS` | `30000` | How long SQLite retries a locked database before failing. |

## Filesystem artifact storage

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_ARTIFACT_STORE_LOCATION` | `.dlboard/artifacts` | Where artifact blobs live. Uploads are staged in `.staging/` inside it, so give it enough room and keep it on one volume. |
| `DLBOARD_FILESYSTEM_STORE_QUEUE_SIZE` | `100` | Uploads each worker process may hold not yet written (`0`: no limit). When it holds that many, new uploads wait up to 30 seconds, then get a 503 the client retries, so an unwritable store turns uploads away rather than filling the disk. |

## Postgres storage

`POSTGRES_STORAGE` and `POSTGRES_S3_STORAGE`.
See [storage.md](plugins/storage.md).

| Variable | Default | |
| --- | --- | --- |
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
| `DLBOARD_POSTGRES_PREPARE_THRESHOLD` | `5` | Set to `null` (or leave it empty, as a Kubernetes `value: null` does) behind PgBouncer's transaction pooling. |
| `DLBOARD_POSTGRES_CONNECT_ARGS` | `{}` | A JSON object of any other libpq parameter. |

## S3 artifact storage

`POSTGRES_S3_STORAGE`; needs the `s3` extra.
See [storage.md](plugins/storage.md).

| Variable | Default | |
| --- | --- | --- |
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
| `DLBOARD_S3_EXTRA_READ_BUCKETS` | none | A JSON array of other buckets dlboard may link and read, never write or delete. |
| `DLBOARD_S3_QUEUE_SIZE` | `100` | Uploads each worker process may hold not yet in the bucket, as `DLBOARD_FILESYSTEM_STORE_QUEUE_SIZE`. |
| `DLBOARD_S3_STAGING_DIR` | `.dlboard/s3-staging` (`/data/s3-staging` in the image) | Where uploads wait until they are in the bucket. An answered upload's only copy is here until then, so keep it on storage that outlives the server, never a tmpfs, and give each deployment its own. |

## Client (training scripts)

Read by `dlboard.client.run.init`, `DLBoardLogger` and the REST client in the training process.
See [client.md](client.md).

| Variable | Default | |
| --- | --- | --- |
| `DLBOARD_API_KEY` | none | An API token (`dlb_...`) from the server's Account page. |
| `DLBOARD_RUN_ID` | none | Attach to an existing run instead of creating one. |
| `DLBOARD_USER` | OS login name | The username to attribute a run to on a server that doesn't verify identity. |
| `DLBOARD_CONNECT_TIMEOUT_S` | `10` | How long a request waits to connect. |
| `DLBOARD_READ_TIMEOUT_S` | `60` | How long a request waits for the server's response. |
| `DLBOARD_CA_BUNDLE` | none | A PEM file of the certificates to trust instead of the system's, for a server with a self-signed or private-CA certificate. |
| `DLBOARD_CONTEXT_GIT` | `true` | Record a new run's commit, branch, `origin` remote and whether tracked files had changes. See [client.md](client.md). |
| `DLBOARD_CONTEXT_DIFF` | `false` | Upload those changes (`git diff HEAD`) as the run's `context/git.diff` artifact. Off unless asked for: uncommitted changes can hold secrets. |
| `DLBOARD_CONTEXT_MAX_DIFF_KB` | `1024` | A bigger diff is left out, with a warning. |
| `DLBOARD_CONTEXT_COMMAND` | `true` | Record the command line, working directory and interpreter. Turn it off if arguments carry secrets. |
| `DLBOARD_CONTEXT_PACKAGES` | `true` | Record each installed distribution's version. |
| `DLBOARD_SYSTEM_METRICS_ENABLED` | `true` | Sample CPU, memory and GPUs into the run, with whichever of the `system` and `gpu` extras are installed. See [client.md](client.md). |
| `DLBOARD_SYSTEM_METRICS_INTERVAL_S` | `30` | Seconds between samples, at least 1. |
| `DLBOARD_SYSTEM_METRICS_GPU` | `true` | Sample NVIDIA GPUs through NVML, when the `gpu` extra is installed. |
| `DLBOARD_CONSOLE_ENABLED` | `true` | Capture what the script writes to `sys.stdout` and `sys.stderr` into the run. See [client.md](client.md). |
| `DLBOARD_CONSOLE_INTERVAL_S` | `30` | Seconds between chunks: also how much of a run's last output a `kill -9` can cost. |
| `DLBOARD_CONSOLE_CHUNK_KB` | `256` | A chunk is cut sooner once this much is waiting. |
| `DLBOARD_CONSOLE_MAX_MB` | `10` | The most a run captures; past it, capture stops and the last chunk says so. |
| `DLBOARD_OFFLINE_DIR` | none | Log every run to this folder instead of a server, to sync later (`dlboard sync`, `dlboard serve local --logdir`). A `file://` server URL does the same for one run. See [client.md](client.md#offline-mode). |

`init` also reads the rank a launcher gives each process, the way Lightning does: the first of `RANK`, `LOCAL_RANK`, `SLURM_PROCID` and `JSM_NAMESPACE_RANK` that is set (torchrun, accelerate, SLURM and jsrun set them).
Only rank 0 logs, and with none of them set a process is rank 0.
GPU sampling reads `CUDA_VISIBLE_DEVICES`, as CUDA does, to sample only the run's own GPUs.

## Shutting down

On SIGTERM (a deploy, `docker stop`, `systemctl stop`), each worker stops accepting requests, finishes the ones in flight, writes every queued artifact blob and records its reference in the database, then exits.
All of that has to fit in `DLBOARD_WORKERS_KILL_TIMEOUT_S`; whatever doesn't is abandoned with a warning, and the client's retry of that upload recovers it.

The orchestrator must wait at least that long before it sends SIGKILL.
Docker's default is 10 seconds, shorter than the default here, so set `docker stop --time` / `stop_grace_period` (the bundled `docker-compose.yml` already does) or Kubernetes' `terminationGracePeriodSeconds` to about 5 seconds more than `DLBOARD_WORKERS_KILL_TIMEOUT_S`.
A server whose artifact writer has died answers uploads with `503` and `Retry-After`, which the client treats as temporary.
