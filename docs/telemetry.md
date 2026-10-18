# Telemetry

## What is this

dlboard can export its own operational metrics (requests, ingest volume, database and artifact-store
latency, sign-ins, ...) for Prometheus to scrape or an OpenTelemetry collector to receive, and can send
its logs over OTLP as well. It can also trace a request from a training script through the server, its database and its
artifact storage. It is built on OpenTelemetry, so anything that speaks OTLP works:
Prometheus 3, an OpenTelemetry Collector, Grafana Mimir, Loki and Tempo, Jaeger, Grafana Cloud, Datadog, ...

This is about the *server*. The metrics your training runs log are a different thing and live in the
database.

## When you'd need this

You run dlboard for other people and want to alert on it: error rates, slow queries, a full artifact
write queue, a spike of failed sign-ins. A local `dlboard serve local` needs none of it, and with no
`DLBOARD_TELEMETRY_*` variable set it does nothing at all: no OpenTelemetry code is imported, no setup
runs, nothing is logged about it.

## How it works

Install the `otel` extra (`pip install 'dlboard[otel]'`; the Docker image already has it), then turn on
what you want with environment variables ([configuration.md](configuration.md#telemetry)):

```bash
DLBOARD_TELEMETRY_METRICS=prometheus,otlp   # either, or both
DLBOARD_TELEMETRY_LOGS=true                 # logs over OTLP
DLBOARD_TELEMETRY_TRACES=true               # traces over OTLP, see Traces below
```

Enabling telemetry without the extra installed is a startup error, not a silent no-op.

The extra asks for OpenTelemetry SDK 1.25 or later, deliberately old so it does not force a newer `protobuf` (1.28 and later
need `protobuf` 5 or later) on whatever else is installed. Logs need 1.38 or later, and say so at startup if the installed SDK
is older.

Telemetry never takes down what it measures. A metric that cannot be recorded, a snapshot that cannot be written or read, a
log event that cannot be forwarded: each is logged once and skipped, and a `/metrics` that cannot add every process up serves
the one that answered instead. Only a misconfiguration (switching it on without the extra) stops startup.

Nothing connects at startup. Each process (every Granian worker, and the artifact writer each one
starts) sets itself up the first time it records something, so a collector that is down or an endpoint
with a typo never stops the server from starting. If setup itself fails, that process logs a warning and carries
on without telemetry, then tries again after 5 seconds, 10, 20 and so on up to every 5 minutes, and logs when it recovers.

A collector that is unreachable *after* setup is the exporter's business, and it recovers by itself too. Metrics are
cumulative, so the next push after the collector is back carries everything counted meanwhile and nothing is lost;
only log events sent during the outage can be, since a failed batch is dropped.

### Prometheus

```yaml
scrape_configs:
  - job_name: dlboard
    metrics_path: /metrics        # under DLBOARD_URL_PREFIX, if you set one
    static_configs:
      - targets: ["dlboard:8050"]
```

dlboard runs several worker processes, and a scrape lands on any one of them. Each process therefore writes
its numbers to a file every `DLBOARD_TELEMETRY_PROMETHEUS_SNAPSHOT_S` seconds (15 by default), and whichever
worker answers adds the files up with its own live numbers. Two consequences:

- Other workers' numbers can be up to one interval old.
- A process that exits has its counters and histograms kept in a single `retired.prom` in that directory,
  so totals never go backwards across a restart or a recycled worker. Only its gauges are dropped.
  Nothing else accumulates, and the whole directory is removed when the server stops, so counters
  start from zero with each server run, which Prometheus treats as an ordinary counter reset.

Newer versions of the OpenTelemetry Prometheus exporter label every series with `otel_scope_name`, `otel_scope_version` and
`otel_scope_schema_url`. They are the same on every process, so nothing changes when the workers are added up.

The workers find each other through a directory that `dlboard serve` and `run_production_server` (the entry point
to call from your own script) set up for you, one per run. If you start the workers some other way (your own Granian or
gunicorn command), point `DLBOARD_TELEMETRY_PROMETHEUS_DIR` at a directory they all share. It is used as it is and not
cleaned up, so use a fresh one per start. Or wrap your own server start in `shared_prometheus_dir()` from
`dlboard.serve.telemetry`, as `run_production_server` does. Without either, `/metrics` reports just the worker that
answered.

`/metrics` is **not behind sign-in**, like `/healthz`. It exposes request counts per route, nothing about
your data, but block it at the [reverse proxy](reverse-proxy.md) or scrape the pod port directly if that
matters to you.

### OTLP

Metrics and logs are pushed over OTLP/HTTP (protobuf; gRPC is not supported). The endpoint, headers, timeouts
and push interval all come from the standard variables:

```bash
# An OpenTelemetry Collector, or Prometheus 3 with its OTLP receiver enabled
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318

# Mimir and Loki directly: each signal has its own URL
OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://mimir:9009/otlp/v1/metrics
OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://loki:3100/otlp/v1/logs

# Grafana Cloud, or anything else that wants credentials
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-eu-west-0.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 of instance:token>
```

`OTEL_SERVICE_NAME` defaults to `dlboard` (set it there, not in `OTEL_RESOURCE_ATTRIBUTES`); add your own labels with `OTEL_RESOURCE_ATTRIBUTES`
(`deployment.environment=prod`). Every process pushes under its own `service.instance.id`, since each
counts only what it saw. Add them up in a query, for example
`sum without (service_instance_id) (rate(dlboard_ingest_metric_values_total[5m]))`.

Logs are the same events as the console or JSON output (at the same `DLBOARD_LOG_LEVEL`), with their
fields as attributes, a `request_id` where there is one, and an exception's type, message and stack
trace as the standard `exception.*` attributes (never its local variables). Only dlboard's own log events
are sent, not those of Granian or Dash.

## Metrics

Names are OpenTelemetry's; Prometheus shows dots as underscores and adds the unit
(`http.server.request.duration` is `http_server_request_duration_seconds`, counters end in `_total`).
Every attribute has a small, fixed set of values. The names and attributes are covered by the
[compatibility promise](compatibility.md).

| Metric | Type | Attributes | |
|---|---|---|---|
| `http.server.request.duration` | histogram, s | `http.request.method`, `http.route`, `http.response.status_code` | Every request, including ones turned away at sign-in. `http.route` is the route template, not the URL. |
| `http.server.active_requests` | up/down counter | `http.request.method` | Requests being handled right now. |
| `dlboard.ingest.metric_values` | counter | | Metric values accepted from training runs. |
| `dlboard.ingest.artifacts` | counter | | Artifacts uploaded. |
| `dlboard.ingest.artifact.size` | counter, By | | Bytes of artifact uploads. |
| `dlboard.artifact.write_queue.rejections` | counter | `dlboard.rejection.reason` (`queue_full`, `writer_down`, `failed`) | Uploads turned away: the write queue was full, the writer was down, or the batch could not be staged. |
| `dlboard.artifact.blob.write.duration` | histogram, s | `dlboard.outcome` | How long the writer took to store one artifact. |
| `dlboard.artifact.purges` | counter | `dlboard.outcome` | Purged artifacts' blobs deleted from the artifact store, or that couldn't be. |
| `dlboard.runs.created` | counter | | Runs started. |
| `dlboard.runs.finished` | counter | `dlboard.run.status` | Runs reported done, by how they ended. |
| `dlboard.auth.sign_ins` | counter | `dlboard.auth.outcome` (`success`, `wrong_credentials`, `throttled`, `disabled`) | Attempts at the password sign-in form. |
| `dlboard.server.workers` | up/down counter | | Server worker processes that started and are serving. One series per process, so it stops arriving when the server is down. |

### Alerting that it's down

A server that never started sends nothing, so its absence is what to alert on, not a value:

- **No worker is reporting.** In Prometheus (or Grafana alerting), `absent_over_time(dlboard_server_workers[5m])`.
  Workers push every `OTEL_METRIC_EXPORT_INTERVAL`, so keep the window several times longer than it.
- **It tried to start and could not.** An error-level log at startup, `dlboard could not start`, carries the
  exception; in Loki, `{service_name="dlboard"} | level="error"`. The same failure is a `dlboard.startup` span marked
  as failed. Waiting for the database or the object store shows as `dlboard.startup.wait` spans with a `retry` event
  per attempt (see `DLBOARD_STARTUP_WAIT_S` in [docker.md](docker.md)).

Neither helps if the collector is down with the server -- a collector on the same host is, after a reboot, for
a while. Only a probe from outside, hitting `/readyz`, covers a whole host being down.

## Traces

A trace shows where the time of one request went. With `DLBOARD_TELEMETRY_TRACES=true` on the server, and in the
training environment too, a batch your script ships is **one trace** from the shipping process to the database and the
blob writer:

```
dlboard.ship                              (client shipping process, one per batch)
└─ POST /api/v1/artifacts                 (client: the HTTP request)
   └─ POST /api/v1/artifacts              (server: the Flask request)
      ├─ SELECT, INSERT, ...              (the database, driver and pool included)
      └─ dlboard.blob.write               (the spawned writer process; S3.PutObject is a trace of its own, see below)
dlboard.blob.ingest                       (a trace of its own: records the refs of a batch of writes, and links to each)
```

The client sends the trace in a standard W3C `traceparent` header, so the server's span continues it; any other
service that speaks OpenTelemetry can join the same way. Each request also carries an `X-Request-Id`, recorded as the
`dlboard.request_id` attribute and printed on the server's log lines for it, so a log line and its trace find each other
(with tracing on, log lines also carry `trace_id` and `span_id`).

Turn it on the same way on both sides. The training environment needs the client's extra
(`pip install 'dlboard-client[otel]'`), the server its own (`dlboard[otel]`):

```bash
DLBOARD_TELEMETRY_TRACES=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
```

The endpoint, headers and timeouts are the standard `OTEL_EXPORTER_OTLP_*` ones, and the sampler is `OTEL_TRACES_SAMPLER`
(default: `parentbased_always_on`, so the server follows whatever the client decided). `OTEL_SERVICE_NAME` defaults to
`dlboard` on the server and `dlboard-client` in your training process. To look at traces locally:
`docker run -p 4318:4318 -p 16686:16686 jaegertracing/all-in-one`, point `OTEL_EXPORTER_OTLP_ENDPOINT` at
`http://localhost:4318` and open `localhost:16686`.

`OTEL_TRACES_EXPORTER` chooses where spans go: `otlp` (the default), `console` (written to stdout, handy for a look
without a collector), both (`otlp,console`), or `none`, which exports nothing while the trace still crosses from client to
server. Any other exporter is a startup error that lists the ones that work.

Spans are exported over **OTLP/HTTP with protobuf only**, to the collector's HTTP port (usually 4318), as metrics and logs
are. gRPC (usually port 4317) is not supported, and an `OTEL_EXPORTER_OTLP_PROTOCOL` (or `_TRACES_PROTOCOL`) of `grpc`
stops startup with a message that says so, rather than losing every span to an export error. A shared environment that
points other services at a gRPC endpoint needs a separate endpoint for dlboard.

The wire format that carries a trace between client and server is the standard `OTEL_PROPAGATORS`, by default W3C
`traceparent` (and `baggage`), on both sides. A mesh that standardises on `b3` or `xray` sets it there, and the two halves
still join.

**What the server instruments.** The server process is dlboard's alone, so it uses OpenTelemetry's own instrumentation,
which keeps up with the libraries and conventions better than anything written here. Flask (every route, Dash callbacks
included) is instrumented per app. Everything else installed is found through the standard `opentelemetry_instrumentor`
entry points, the mechanism `opentelemetry-instrument` uses: SQLAlchemy, `psycopg` and `botocore` come with
`dlboard[otel]`, and **installing `opentelemetry-instrumentation-<library>` is all it takes to also trace a plugin's own
storage or auth client**. `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=botocore` switches any of them off.
`OTEL_PYTHON_EXCLUDED_URLS` (or `OTEL_PYTHON_FLASK_EXCLUDED_URLS`, which replaces it for Flask when set) adds URLs
(regular expressions) not to trace. Health probes, `/metrics` and static files never are.

Every statement dlboard runs goes through SQLAlchemy, whose instrumentation traces it, on SQLite and on Postgres alike.
A database driver's instrumentation (`psycopg`) would trace each of them a second time, so it is left off while
SQLAlchemy's is on. `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=sqlalchemy` swaps them: SQLAlchemy's off, the driver's on, for
spans at the driver (its connections and cursors) instead.

dlboard asks SQLAlchemy's instrumentation to run on SQLAlchemy releases newer than the ones it has been
tested with, since it only listens for statements, which don't change between them. HTTP attributes use the stable
names (`http.request.method`, `http.response.status_code`), the same as the client's, unless you set
`OTEL_SEMCONV_STABILITY_OPT_IN` yourself.

**Running under `opentelemetry-instrument` or the Kubernetes Operator.** A platform that sets OpenTelemetry up for you
gets one pipeline, not two. If the process already has an SDK tracer provider when dlboard starts, dlboard's own spans
(`dlboard.blob.write`, `dlboard.blob.ingest`, `dlboard.artifact.purge`) go through it, with or without
`DLBOARD_TELEMETRY_TRACES`, and the libraries the platform already instrumented are left as it set them up. Without one,
and with `DLBOARD_TELEMETRY_TRACES` on, dlboard's provider becomes the process's global one, so a plugin that calls
`trace.get_tracer(...)` reaches it. This is the server only: the client never replaces a provider in your training process.

**What the client instruments.** Only dlboard's own requests, written by hand: the client never patches `requests` or
installs a global tracer provider in your training process, so your own OpenTelemetry setup (or lack of one) is not
touched. Its OpenTelemetry packages are the standard `opentelemetry-sdk` and OTLP exporter alone, from release 1.16 on,
with no upper bound (and no contrib packages, which pin an exact SDK release), so a training environment's own versions
stay as they are. The calls the training process makes itself (`whoami`, creating the run, finishing it) are spans too,
in traces of their own.

**What is never recorded.** The values bound into a SQL statement (the statement has placeholders), request and response bodies,
and the `Authorization` header. HTTP headers are not recorded at all unless you ask with
`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST` and the like, and then `Authorization`, `Cookie` and
`Set-Cookie` are redacted unless you set `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS` yourself. An
exception is recorded with its type, message and traceback, never its local variables. The message is the library's own,
so a database error is recorded as the database wrote it, the same text as in the server's log: Postgres's `DETAIL` line
can name a value from the row it rejected (`Key (name)=(...) already exists`). The values bound into the statement are not
in it.

**It never gets in the way.** Spans are exported in batches in the background, and the queue is bounded: a collector that
is down or slow costs spans, never a request, never a training step. Setup happens the first time something is
traced, nothing connects at startup, and a tracing failure is logged once and otherwise ignored. A shipping process
exports its last spans when it is told to flush, before `finalize()` returns, and a process that is exiting waits at most about
ten seconds for a collector that will not let go (a rate-limiting gateway can ask an exporter to back off for a minute).

Three things to know about what you will see.

- Every Dash callback is a request, so each is a span named `POST /_dash-update-component`, with the sign-in check's queries
  under it. On a busy server that is most of the volume, and it says little about which callback it was. Sample it down with
  `OTEL_TRACES_SAMPLER=parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG=0.1` on the server. A batch the client ships
  keeps the client's decision, whatever the server's ratio.
- On S3 storage the upload's requests (`S3.PutObject`) are traces of their own, not children of `dlboard.blob.write`: boto
  uploads from threads of its own, which do not carry the trace, and dlboard leaves how an upload runs alone rather than
  change it for the sake of the trace.
- A batch the client could not ship (the server was down) is retried, each attempt a trace of its own.

## How do I add one

The package is public (`dlboard.serve.telemetry`), so a plugin records its own metrics the way dlboard does. A metric is
a value defined at module level, which costs nothing and can't fail, so the code that records it stays one line:

```python
from dlboard.serve.telemetry import Counter, NoAttributes, Unit

_SAVED = Counter[NoAttributes]("myplugin.saved", Unit.ARTIFACTS, "Things my plugin saved.")

_SAVED.add(1)
```

Labels are a frozen dataclass, so a wrong label or type is caught by the type checker and the set of series stays
bounded. Name each label with `attribute(key)`, where `key` is a member of a `StrEnum` of label names (dlboard's own are
`AttributeKey`; a plugin defines its own, ideally using OpenTelemetry semantic-convention names), and use enums or ints
for the values:

```python
class _Label(StrEnum):
    KIND = "myplugin.kind"


@dataclass(frozen=True, slots=True)
class _SavedAttributes(Attributes):
    kind: Kind = attribute(_Label.KIND)


_SAVED = Counter[_SavedAttributes]("myplugin.saved", Unit.ARTIFACTS, "Things my plugin saved.")

_SAVED.add(1, _SavedAttributes(kind=Kind.IMAGE))
```

With telemetry off, `.add()` is a single comparison, and whatever goes wrong inside it is logged once and
swallowed. To count what a handler streams through without reading it twice or holding it, wrap the
iterable in a `Tally` and read `.total` once it has been consumed.
