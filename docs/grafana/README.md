# Grafana dashboards

Three dashboards, one per signal dlboard exports:

| File | Data source | What it shows |
|---|---|---|
| `dlboard.json` | Prometheus | The server's metrics: requests, ingest from training runs, the artifact pipeline, experiment page reads, runs and sign-ins. Each panel's query uses a metric from [telemetry.md](../telemetry.md), and its description says what a bad value means. |
| `dlboard-logs.json` | Loki | Error log lines, a "could not start" count, and the server's log lines. |
| `dlboard-traces.json` | Tempo | Failed traces and the slowest uploads, store commits, page updates and panel reads, found with TraceQL. |

The metrics dashboard needs a Prometheus that has dlboard's metrics: either one scraping `/metrics`
(`DLBOARD_TELEMETRY_METRICS=prometheus`), or one receiving them over OTLP (`DLBOARD_TELEMETRY_METRICS=otlp`).
The logs dashboard needs `DLBOARD_TELEMETRY_LOGS=true`, and the traces dashboard needs `DLBOARD_TELEMETRY_TRACES=true`.

## Import them

In Grafana: **Dashboards → New → Import**, upload the file, and pick the data source for its input (**Prometheus**,
**Loki** or **Tempo**). Each file has its own uid (`dlboard-overview`, `dlboard-logs`, `dlboard-traces`), so importing
one again replaces the copy already there.

For the logs dashboard's trace links to work, the Loki data source needs a derived field on `trace_id` that points at the
Tempo data source. That's a setting on the data source, not in the dashboard.

## Provision them

To have Grafana load them on startup, put them in a folder Grafana reads and add a provider:

```yaml
# provisioning/dashboards/dlboard.yaml
apiVersion: 1
providers:
  - name: dlboard
    folder: dlboard
    type: file
    options:
      path: /var/lib/grafana/dashboards/dlboard
```

Then mount the files into that path. The data source inputs are filled in by the import step, so a provisioned
dashboard needs each `${DS_...}` placeholder replaced with that data source's uid first, or a `datasource` variable
added and the panels pointed at it.

## Keeping them up to date

The dashboards are files in this repo, so changes go through review like any other change:

1. Edit the dashboard in Grafana, then **Export → Export as JSON** with "Export for sharing externally" on. That keeps the
   input block that makes it importable.
2. Save the result over the file it came from, and commit.

When a metric, log label or span name is renamed or added, update the panels that use it. The metrics queries are written
against the Prometheus names of the metrics in [telemetry.md](../telemetry.md): dots become underscores, counters end in
`_total`, and the unit is appended (`_seconds`, `_bytes`). The log queries use the `service_name` label and the `level`
and `trace_id` fields; the trace queries use span names from the same document.

## Caveats

- The `dlboard_ingest_artifact_size_bytes_total` name follows the convention above, not a checked name. Confirm it on a
  live `/metrics` before relying on the "bytes per second" series.
- Histogram panels use `histogram_quantile` over `rate(..._bucket[$__rate_interval])`. A low-traffic server shows
  noisy quantiles; widen the time range if a panel looks jumpy.
- The logs stat counts "could not start" lines over the whole selected time range, so it stays red for that range after
  one failure.
- None of these have been loaded into a Grafana instance from this repo yet. Check each one after importing.
