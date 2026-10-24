# Changelog

All notable changes to dlboard are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows the tiers described in
[docs/compatibility.md](docs/compatibility.md).

<!--
To cut a release: rename this section to `## [X.Y.Z] - YYYY-MM-DD`, start a fresh empty
`## [X.Y.Z]` above it, bump `dlboard/_version.py`, and tag `vX.Y.Z` -- the release workflow
pulls that section's body into the GitHub release notes and publishes both distributions from it.
-->

## [Unreleased]

### Added

* **A chart can show how many metrics' values spread, as a band.** The series band chart draws, for the metrics its names
  and patterns match, the median of their values at each step between their quartiles, inside their extremes, for the
  newest run (up to three). It reads a grid of at most 64 steps (32 by default), so a band over thousands of metrics costs
  one read per metric, run and grid step, not one per step a run trained.
* **A metric pattern can have its `*` anywhere.** `layer_*/grad_norm` names every metric of that shape, including ones
  logged later. A pattern used to end in `*`.
* **`dev_harness.py seed` can log many metric keys, and times the upload.** `--metric-keys`, `--key-template` and
  `--steps-per-batch` shape the upload, and the output reports the rows, the ingest rate, the size of the first request
  and the size of the database.
* **A chart plugin can ask for the spread of many metrics' values.** `hint_required_series` hands the chart a
  `SeriesBand` per run and step: the minimum, quartiles, median, maximum and mean of the values its patterns match,
  read on a bounded grid of steps. The series band chart is built on it.

### Changed

* **Auto-generate charts a family of metrics as one band.** Metrics whose names differ only in their numbers
  (`layer_0/grad_norm` to `layer_99/grad_norm`) are charted as one band once there are at least 12 of them, rather than a
  chart each. A family of 11,000 metrics is one chart.
* **A table of a metric pattern reads only the metrics it shows.** A table of `train/*` used to read every metric in the
  experiment.
* **Listing an experiment's metric keys no longer judges each key's shape.** The chart editor, the auto-populate preview,
  the suggestions drawer and the run table list names only, and a key's shape is judged only for the keys a chart is made
  from. On an experiment of 11,000 keys and 20 runs (SQLite) the listing took 346 ms and now takes 35 ms.
* **The suggest-charts drawer lists the first 50 suggestions, and says how many the filter leaves.** It no longer keeps
  every suggestion's chart in the browser, which froze with thousands of metrics, and builds a chart only when it is added.
  "Select all" still ticks every suggestion the filter leaves.
* **The key pickers list the first 100 names that match what is typed.** They still search every name.
* **A step that logged thousands of keys is uploaded in pieces of at most 1,000 keys.** Each queued item carries at most
  1,000 keys, so a batch of items is a bounded upload however wide the steps are.
* **Auto-generate makes at most 200 charts at once.** Keys that share no name used to make a chart each, so a catalog
  of thousands made a page of thousands of charts. The keys past the limit stay uncharted, so Suggest charts can still
  add them, and the auto-populate preview says how many it leaves out.
* **Reading many metric keys at once is faster.** The latest value of each key, and a run's whole history, used to cost
  a pandas pass over every key column. For one run of 32 steps in an experiment of 11,000 keys (SQLite), the latest
  values took 1.6 s and now take 0.14 s, and the history 2.5 s and 1.1 s. An experiment of 20 keys is unchanged.
* **The run comparison lists its keys 50 at a time.** Each page reads only its own keys' values: for five runs of 11,000
  keys (SQLite) a page takes about 20 ms, where every change of the runs read every key (0.8 s) and sent every row to the
  browser (1.9 MB). The search matches key names and hyperparameter values; a metric's values are no longer searched,
  since that needed every key's latest value on each keystroke.

### Fixed

* **Reading the latest or first value of more than 500 named metric keys no longer fails on SQLite.** The read unioned one
  SELECT per key, and SQLite refuses a union of more than 500.
* **A run's latest value no longer costs more as the run trains, on Postgres.** The check that a value was the newest
  logged at its step read the key's whole series for every run, so the run comparison and the latest-value panels read
  more pages the longer a run trained.

## [0.11.0] - 2026-10-23

### Changed

* **A line or bar chart no longer has a y-axis label**: its heading already names the metric, so the axis shows only its
  values and the plot gets the room the label took. A bar chart's heading now says how its values are grouped
  (`median(loss)`), which the label used to.
* **The experiment page makes two fewer requests when it loads.** The run table reported its own mount as a change of
  selection to the server, twice, and the server answered that nothing had changed; the browser now compares the
  selection with the runs drawn and asks only when they differ.
* **The experiment page no longer reads everything that was logged to show what it summarizes.** The lists of
  metrics and artifacts, where each run ended up, and a bar chart or a table with a row per run used to cost more with
  every step a run trained (and the lists, with every experiment in the database). In a Postgres with 3.6M metric rows
  the chart editor's list of metric keys took 1.7 s and now takes about 10 ms. On the shape of
  `examples/lightning_advanced.py` (two runs, 56k rows, SQLite) the chart editor's catalog went from 59 to 8 ms, the run
  comparison from 569 to 9 ms and a table of every metric from 614 to 27 ms.
  * **Indexes.** The metric and artifact tables are indexed by how they are read, `(experiment_id, key, run_id, step)`,
    as the histogram table already was; the old ones led with `key`, so every read of one experiment scanned every
    experiment's rows. The new ones are narrower than the old, so the database gets smaller. Runs and
    hyperparameters are indexed by experiment too (hyperparameters had no index, so every page scanned every
    experiment's), and each per-step table is indexed by run too, so deleting or purging a run no longer scans them.
    **Starting the server after upgrading builds them** (migration 0004): on Postgres it builds each new index before
    dropping the one it replaces, so reads of the metric and artifact tables wait only for the swap at the end, but
    writes to a table (a run logging metrics) wait while its indexes are built. On a database with many millions of
    metric rows the builds take a while, so upgrade at a quiet moment. A server still running the old version keeps
    working against the upgraded database during a rolling upgrade, only reading as slowly as it did (as with every
    migration, one that is stopped cannot be started again on it).
  * **Which keys an experiment logged**, whether a metric is logged once per run, and the latest artifact of each key are
    found by hopping through the index from key to key, not by counting and sorting every row. Whether a metric is
    logged once per run is judged on the newest 100 runs, and the column catalog's hyperparameter keys are read from
    those runs too, so these do not cost more as an experiment gains runs.
  * **Where each run ended up**, in the run comparison and the Runs table's metric columns, is one probe of the index
    per run and metric, and **a bar chart, and a table with a row per run,** are handed a row per run (the latest,
    first, minimum, maximum or mean of each metric, as the table's aggregate says) instead of every step to reduce in
    pandas. A bar chart now takes each run's latest x and latest value rather than needing a step that logged both:
    the same for a hyperparameter x-axis, and no longer an empty chart for two metrics logged at different steps.
    The "fetches all metrics" badge only shows for an unscoped chart that still reads every step.
  * **A line chart's history is cheaper per point** (a panel of 50 lines on that same data, 592 to 265 ms). It still
    reads every point logged of its metrics, so how often you log decides how fast it draws:
    [docs/client.md](docs/client.md) says how to log less.
* **Logging metrics is faster, and an ingest's time shows in its trace.** A batch's metric and histogram rows are written
  in one transaction (one commit, not three), and on Postgres with COPY, not one round trip per row. On a local Postgres (WSL2),
  a batch of 10,000 steps of four metrics went from 4.7 s to 3.1 s; what is left is the database
  writing each row and its index entries to its log, which a batch cannot avoid. The ingest's trace now has spans for
  parsing the batch (`dlboard.metrics.parse`), building its rows (`dlboard.store.rows`) and writing them
  (`dlboard.store.insert`), so the time that is not a SQL statement is visible too.
* **An experiment page draws its newest 20 runs until told otherwise, and the Runs table lists every run.** Runs are the
  one thing an experiment can have thousands of, and a user does not control how many. The charts used to fetch every
  run but the few a view left out, and the table stopped at 1,000 runs, so run 1,001 could not be reached. Now a page
  reads only the runs it draws: the newest 20 (`DLBOARD_DEFAULT_SHOWN_RUNS`), or the ones selected in the Runs table,
  whose checkboxes decide what the charts draw. The table filters by name as well as by status and hyperparameter, and
  its header checkbox selects only the runs a filter matches. A selection of more than `DLBOARD_MAX_SHOWN_RUNS` (100)
  runs is refused with a notice, so no view can read every run of an experiment of thousands. A saved selection
  leaves out the runs logged after its newest run, and the table says how many. Once runs are selected a **Newest 20**
  button above the table returns the view to the default. A view saved before selections, with runs left out, still
  loads and draws the newest 20 of the runs it did not leave out; Newest 20 clears its exclusions. With 2,000 runs
  (800k rows, SQLite) three line charts take 38 ms (44 ms at 100 runs) and run compare 10 ms.
* For chart plugin authors: **`ChartType.metric_reductions`** says how much of each metric a chart draws from, by
  column (`MetricReductions`: a `default`, and a `by_column` for the metrics that differ), each a row per run
  (`MetricReduction.LATEST`, `FIRST`, `MIN`, `MAX` or `MEAN`) or every step. It defaults to every step, so a chart
  type that does not say is unchanged. See [docs/plugins/charts.md](docs/plugins/charts.md). For storage plugin
  authors: reads of what runs logged take the runs to include, `run_ids`, instead of the runs to exclude, and it is
  required; `fetch_metrics` takes a `reduction`, which is a hint a store may ignore; `summarize_artifact_keys` is new;
  and the key summaries' `MetricKeySummary.max_steps_per_run` is an upper bound on the steps a run logged, exactly 1
  when no run logged two. `SQLStoreBase` implements them for SQLite and Postgres, the two dialects it is tested on.

## [0.10.0] - 2026-10-21

### Added

* **Segmentation masks as overlay layers.** `dlboard.client.artifacts.rle.run_length_encode` turns the class of each pixel
  of one picture (an integer array of `height width`) into an `ImageMask`, run-length encoded with the mask's shape and
  each class's color. Logged in an `Overlay`, an
  image chart paints it over its picture, at the mask's `opacity` (0.5 by default, so the picture shows through): a class
  with a color is drawn in it, and a class without one is left see-through. With no `colors` at all, a mask draws class 0
  clear and every other class in a built-in palette (an empty `colors={}` draws nothing). An older server stores it
  without drawing it, so upgrade the server to see masks.
* **A table chart draws the overlays logged over a picture on its thumbnail**, as an image chart does, in a table of
  runs, of a metric's values or of artifacts. An overlay of a picture the table shows is no column or row of its own.
* **A legend over each of an image chart's pictures.** It lists the layers drawn on the picture, a row each: tick one to
  show or hide it on every picture at once, without loading anything again, or shift-click it to show it alone, to
  compare a truth with a prediction. Each row lists what its layer holds (the classes of a mask, the labels of shapes) in
  their colors, so a class of one layer is not mistaken for another's. The eye hides every layer, and holding it or a
  picture shows the picture without them while held; the legend starts folded to "Layers" and opens on a click, and a layer the picture has nothing
  for is dimmed. What a viewer changes is their own, kept for the browser tab and for the view of the experiment they
  are looking at: the chart's card says "View changed" and
  offers **Save as default**, which writes which layers are shown into the chart's settings (into a view of your own, if
  the page is not yours), or **Reset**.
* **The chart editor is docked beside the panels**, not a modal over them, so the rest of the page stays in view. An
  edited chart is drawn as the form has it in its own card, in place of the saved chart; Cancel puts it back exactly as
  it was, and a live update never overwrites the draft. A chart being added is previewed in the pane. Its settings are
  grouped into collapsible sections (Data, Display, and a chart type's own, such as Layers and Tags on an image chart).
* For chart plugin authors: **`ChartType.fixed_table_rows`** lists in the editor's table settings the rows a chart draws
  whether or not they are saved (restyleable, but not renamed or removed), **`SettingsSection`** (with `DATA_SECTION`,
  `DISPLAY_SECTION` and `ADVANCED_SECTION`) groups a chart type's settings in the editor, and a setting's
  `Field(title=...)` is what the editor calls it when its name as a sentence is not ("Show hyperparameters", not "Show
  hparams"). See [docs/plugins/charts.md](docs/plugins/charts.md).

### Changed

* **Upgrade every server before editing an image chart.** A chart saved with `layers` does not load on an older server,
  which shows it as an error, and an edit made there would drop its layers.
* **An image chart draws every overlay logged over its image**, including ones logged after the chart was made, rather
  than only the keys listed in its `overlays` setting. The setting is replaced by `layers`, a row per layer (an
  overlay by its name, any other artifact by its key) with a `visibility` (`shown`, `hidden` or `off`) and an `opacity`;
  a chart saved with `overlays` opens as the rows that draw the same layers. One that left out an overlay of its image
  now draws it (set it `off`), and an image's own overlays are drawn in key order. **The chart editor's Layers table lists
  every layer the chart draws**, rather than only what was saved, so a layer can be hidden, turned off or added there.
  An overlay's row is the chart's to keep: it can be restyled, but not renamed or removed (set it `off` to leave the
  overlay out), and one left as it is by default is not saved.
* Overlays are shown by their image and name (`samples/a · truth`; `unnamed overlay` when it has none) in an image chart's title, a
  table's column headers and the chart editor, never by the `_$overlay` suffix they are logged under. The editor's key
  pickers no longer offer an overlay whose image is logged, since that image's chart draws it.
* The chart editor labels each setting as a sentence ("Tag precision", "X axis"), not by its field name; a chart type's
  `ParameterField.label` is derived from the name, so plugins get it with no change.

### Deprecated

* `dlboard.models.overlay_image_key` is kept for one release, as the image an overlay key is of. Use `OverlayKey.parse`,
  which also gives the overlay's name.

### Fixed

* **Editing a chart keeps its id**, so a `?chart=` link to it still opens it. It used to be derived from the new
  settings again on every edit. A new chart now gets an id of its own, so two charts of one config are not one chart to
  a link.
* Switching the chart type in the editor no longer fills the new form from the old type's settings where a name
  (`height`) happens to match, and a number input in the editor is bounded by what its setting allows.

## [0.9.0] - 2026-10-19

### Added

* **Image overlays.** `dlboard.client.artifacts.overlay.Overlay` logs the shapes drawn over a picture -- boxes and
  polygons, each with an optional label and color -- as a small JSON file, at the same step as the picture, so an
  annotated photo can be a small JPEG rather than a large PNG with the boxes drawn in. `Overlay.over("sample", ...)`
  logs it under `sample_$overlay` (give several of them a `name` each), and the image chart auto-generate makes for
  `sample` draws them over each picture, in its thumbnail and its enlarged view, with no chart of its own for the
  overlays. See [docs/client.md](docs/client.md).
* **Layers over an image chart's pictures.** Its new `overlays` setting is a row per artifact key, bottom layer first,
  each with an `opacity`. A layer is an overlay of shapes or any image, such as a segmentation mask, logged at the
  picture's own step; the chart editor suggests the keys the experiment logged that can be one as a row's name is typed, and sets the opacity with a slider. An older server stores an overlay without drawing it, so upgrade the server to see them.
* **Distributed traces** (`DLBOARD_TELEMETRY_TRACES=true`, with the `otel` extra on the server and `dlboard-client[otel]`
  in the training environment). A batch your script ships is one trace, from the shipping process through the server,
  its database queries and the blob writer, joined by a W3C `traceparent` header (calls to S3 are traced too, as traces of their own).
  The server uses OpenTelemetry's own instrumentation for Flask, SQLAlchemy and botocore, and picks up any other
  `opentelemetry-instrumentation-*` package you install, so a plugin's own client is traced too. Requests carry an
  `X-Request-Id`, recorded on the span and the log lines (which also gain `trace_id` and `span_id`). The client needs only
  the standard OpenTelemetry SDK and OTLP exporter (1.16 or later, no upper bound) and patches nothing in your training
  process. Bound SQL values and credentials are never recorded. See [docs/telemetry.md](docs/telemetry.md#traces).
* The **`docker compose` trial now also runs Grafana's `otel-lgtm` image** and sends dlboard's metrics, logs and traces to
  it, so Grafana at <http://localhost:3000> shows them with no setup. The environment it takes is in
  [docs/docker.md](docs/docker.md#watching-it-in-grafana).

### Changed

* An image chart's thumbnail shows a **spinner** while the picture for the slider's step is still downloading. The old
  picture is dimmed under it, rather than shown as if it were that step's, and the spinner goes once the new picture
  has arrived.
* Changed default location for logged data to the current directory for sqlite instead of the home directory to better
mirror tensorboard behavior.

### Fixed

* **An image chart keeps working after you edit it.** Its slider and enlarged view used to stop responding until the
  page was reloaded, because the chart editor's preview rendered the chart under the very ids the saved chart got, and
  Dash cannot resolve a callback with two components under one id. A chart's ids now include its own id, so the preview,
  the saved chart, and two charts of one config no longer collide.
* A failing SQL statement's **error text no longer carries the values bound into it**, so a password, a token or a run's
  name that was a parameter of the statement is no longer in the server's log lines (or a trace's spans) when the
  database rejects it.
* A run's default name is no longer the same for every run a **seeded training script** starts. Names were drawn from the
  global `random` module, so `seed_everything` gave each run the same name, and naming a run also shifted the script's
  own random draws. Names are now drawn from the OS.
* **The server survives starting before its database is up**, as every container does after a host reboot (Compose's
  `depends_on` only orders `docker compose up`). It used to give up on the first refused connection, and Granian took
  the second quick death for a crash loop and exited, leaving a stopped server that nothing restarted. The Postgres and
  S3 plugins now retry an endpoint that is not reachable *yet* for `DLBOARD_STARTUP_WAIT_S` (120 seconds), with a log
  line and a trace event per attempt; a wrong password or a missing bucket still fails at once. The compose file also
  sets `restart: unless-stopped`.
* **A server that cannot start now says why** as one `dlboard could not start` error, with its exception, on a failed
  `dlboard.startup` span, instead of two interleaved tracebacks on stderr that no log collector saw. The new
  `dlboard.server.workers` metric disappearing is what to alert on: see [docs/telemetry.md](docs/telemetry.md#alerting-that-its-down).

## [0.8.0] - 2026-10-16

### Added

* A **table chart's rows are a setting you choose**: a run, each value of a metric (`step`, `epoch`, ...), or each
  logged artifact. With rows of a metric, `spread_by_run` puts the runs side by side, so "a row per epoch, each
  run's latest image of that epoch" is one table. What a run logged between two values of the metric belongs to
  the earlier one. A metric can also be reduced to its first, minimum, maximum or mean (`aggregate`) instead of
  its latest. See [docs/plugins/charts.md](docs/plugins/charts.md).
* A table's metrics, hyperparameters and artifacts take **names or a prefix and `*`** (`eval/*`, or `*` for all).
  In the chart editor you can type a prefix instead of picking from the keys logged so far, and a prefix also
  matches keys logged later.
* **Histograms.** `DLBoardLogger.log_histograms({"grad_norm/layer1": tensor}, step=...)` logs the distribution of
  any array, tensor or list of numbers at a step -- per-layer gradient norms, weights, per-sample losses --
  instead of averaging it into a scalar. The values are binned **in your training process, at the call**, so only
  bucket edges and counts (at most `DLBOARD_MAX_HISTOGRAM_BUCKETS`, 1024 by default) are sent and a bad binning slows
  down only the script that chose it. `ExponentialBinning` (the default) puts a fixed number of buckets per power of ten on a lattice that does
  not depend on the data, so steps and runs line up bucket for bucket; `ExplicitBinning` takes the edges you
  give. See [docs/client.md](docs/client.md).
* A **histogram chart** shows them as a heatmap over steps, as percentile bands, or one step at a time with a
  slider, a row per run in that run's color. A chart reads about `DLBOARD_MAX_HISTOGRAM_STEPS` (256) steps per run however long it
  trained, thinned in the database by a power-of-two stride, so it costs the same to open after a million steps as
  after a thousand. Its colorscale, the share of values its bands hold, and how many rows and slider steps it draws
  are chart settings, and its accent color and spacing are `DLBOARD_HISTOGRAM_*` settings
  ([docs/configuration.md](docs/configuration.md)). Auto-generate and Suggest charts make a heatmap for each
  histogram key. See [docs/plugins/charts.md](docs/plugins/charts.md).
* A database migration (`0003`) adds the histogram table. A histogram's bucket edges and counts are stored as packed
  binary arrays (8 bytes an edge, and a count in as few bytes as it fits), not as JSON text: about a third of the
  space on SQLite, and none of the cost of printing and parsing every float. A server older than this ignores the
  histograms a newer client sends, so upgrade the server first.

### Changed

* A table chart's `mode` (`runs`, `pivot`, `artifacts`) is replaced by `rows`, `row_field`, `spread_by_run`,
  `show_metrics`, `show_hparams` and `show_artifacts`. A table saved with the old settings opens as the new ones that
  show the same table, in the chart editor too.
* **Everything the server prints now goes through one logging setup.** Granian's own lines, and any library that
  uses the standard `logging` (Flask, Werkzeug, SQLAlchemy, ...), are rendered like dlboard's own, so
  `DLBOARD_LOG_FORMAT=json` is JSON throughout, `DLBOARD_LOG_LEVEL` governs Granian's too, and
  `DLBOARD_TELEMETRY_LOGS` forwards them. A library's records appear from `WARNING` up.

### Fixed

* A table chart can show **no hyperparameters** (or no metrics): an empty list used to mean "all", so choosing
  only an artifact brought every hyperparameter and metric along. New tables still start with all of both.
* **An error the server didn't handle is logged where you will see it.** It used to be left to Flask's own logger:
  unstructured, with no request id, and absent from JSON and OTLP logs. It is now one event, with the method, path
  and `request_id`, and the client's error for that request names the same id (`server request id ...`), so a
  failed batch points at its log line.
* Starting the server no longer prints `RuntimeWarning: Repeated configuration attempted` once per worker.

## [0.7.0] - 2026-10-14

### Added

* A line or bar chart is titled with its full metric name, above the chart. The y-axis title is still
  shortened to fit, so a long name was hard to read.
* A **table chart can show artifacts**. In its new `artifacts` mode it lists one row per logged artifact under a
  key, or every key under a prefix (`eval/`, including keys logged later), with each tag as a column that
  sorts and filters as the number or text it is -- images as thumbnails, other files as download links. In
  `runs` mode its `artifacts` setting adds a column per key with each run's latest artifact. A tag measured in
  a unit is shown in its base unit (seconds, bytes) in a table, so runs compare. See
  [docs/plugins/charts.md](docs/plugins/charts.md).
* A table chart's **`mode`** (`runs` or `pivot`, besides `artifacts`) is now a setting of its own instead of
  being implied by `pivot_on`; a table saved with `pivot_on` and no `mode` still loads as a pivot, and a pivot
  without `pivot_on` and `pivot_metric` is a form error in the chart editor.
* A table chart names and colors its runs as the charts and the Runs table do (a pinned **Run** column, run
  names as pivot headers), groups its columns, filters in place, and **keeps its sort, filters and page when a
  live update redraws it** (it used to start over every time).

### Fixed

* A table chart showed each run's *last row* rather than each metric's own latest value, so a metric logged
  less often than the last step (a per-epoch `val_acc` beside a per-step `loss`) was blank; it now agrees with
  the Runs table. It also no longer shows `step` as a metric, shows only hyperparameters when only those are
  chosen, keeps an hparam and a metric of the same name apart, and reads a metric named `a.b` as one column.
* Numbers in tables: whole numbers are shown as they are (not `12.000`) and small ones to four significant
  digits (a learning rate of `1e-4` was `0.000`).
* A live update redrew charts it should not have (another run logging a step renumbered the rows a chart was
  compared by) and missed ones it should have (a hyperparameter edited, a run renamed).

### Changed

* **Which panels are open and which tab is shown are part of the page's address** (`?open=`, `?tab=`), like the
  page of panels already was, instead of being saved on the experiment's page or view. Opening a panel used to
  open it for everyone else looking at the same page; now a link you share shows what you were looking at, and
  a fresh visit opens the first panel of the first tab. Pages saved before still load -- the open panels and
  tab they recorded are just no longer read.
* Changing anything about the shared page, its settings included (the runs it leaves out, its Runs-table
  columns), now takes an editor, as changing its panels already did. A viewer's edit still branches into a view
  of their own.

## [0.6.0] 2026-10-13

### Added

* A panel's **Sync crosshair and step across charts** setting now also locks its image-series sliders
  together: moving one moves the others to the latest step each has at or before it.
* An image-series chart is titled with its artifact key.
* A tag can carry a unit: `tags={"took": Quantity(value=5400, unit=TimeUnit.SECONDS)}` records what the
  number was measured in (time `ns` to `d`, size `B` to `TB`, decimal); on the wire it is
  `{"value": 5400, "unit": "s"}`. The client never says how to show it: the image chart shows it in the
  largest unit that fits by default (`1.500 h`, `2.500 GB`, not `5400` or `2500000000`), or in the unit set
  by its new `tag_time_unit` / `tag_size_unit` settings, to `tag_precision` significant digits, and
  `tag_display` overrides those per tag name. Plain
  numbers are written with separators (`12,346`) instead of `1.235e+04`.
* Opt-in OpenTelemetry for enterprise deployments. `DLBOARD_TELEMETRY_METRICS=prometheus,otlp` exports the
  server's own metrics (HTTP requests and latency, metric and artifact ingest, the artifact write queue, run
  lifecycle, sign-ins, artifact purges) either as a Prometheus scrape of `/metrics` -- added up across every
  worker -- or pushed over OTLP to a collector, Mimir or Grafana Cloud, and `DLBOARD_TELEMETRY_LOGS` sends the
  logs over OTLP too (Loki). It is the new `otel` extra (OpenTelemetry SDK 1.25 or later, 1.38 for logs),
  installed in the Docker image; with none of it set nothing is imported or logged, and every process (each
  Granian worker and its artifact writer) sets itself up on first use, so a collector that is down never stops
  the server from starting. Telemetry never takes down what it measures: a failure to record is logged once and
  skipped, and a setup that failed is retried with a back-off. The metrics API is public, as
  `dlboard.serve.telemetry`, for plugins to record their own. See [docs/telemetry.md](docs/telemetry.md).
* Docker images between releases: `ghcr.io/dlangerm/dlboard:beta` follows main (and `sha-<commit>` pins a
  single landing), `:latest` is the same tested `main`, and `:stable` follows the newest release.
  See [docs/docker.md](docs/docker.md).

### Fixed

* Live updates replaced a chart's whole item with a whole new item nested inside it (a second set of
  controls and padding) the first time its data moved, and redrew *every* open chart on the first update
  after a page loaded, including ones whose data had not changed. A chart now carries a fingerprint of the
  data it was drawn from, so a poll replaces only the chart inside its item, and only when its data moved.
* A request the sign-in gate turned away (a `401`, or the redirect to the sign-in page) had no request id, and
  with `DLBOARD_ACCESS_LOG` on its response raised an error instead of being logged.
* Pressing Ctrl+C during training left the process hung for a minute per unshipped queue, with "still hadn't
  all been shipped" and "shipping process is dead" warnings. The terminal's Ctrl+C reached the logger's
  background shipping processes too, so they died before the final flush could send what was queued. They now
  ignore Ctrl+C, and the training process flushes and exits.

### Changed

* **The `latest` Docker tag no longer means "the newest release"**: it now follows `main` (the same as
  `:beta`). Pin `:stable` (or a version, `:0.5.0`) for releases only.
* An image-series chart lays its runs out by what its panel and page say: as many columns as the panel's
  grid column count (following it the instant it changes) and as many runs per page as the viewer's
  charts-per-page size, instead of a fixed three and six. It never uses more columns than it has runs, so one
  run's image fills the chart's width and a large chart size now enlarges it.
* Artifact `tags` values are JSON scalars (`str | int | float | bool`) on the wire and in storage, not
  text (#179): `NewArtifact.tags` is `dict[str, TagValue]`, clients send what the caller logged
  (`tags={"score": 0.14}`), and the image-series and file-list charts format numbers the same way wherever
  they come from. **A server older than this release rejects a numeric tag with a 400**, so upgrade the
  server before the clients; existing string tags are unaffected. This replaces the text-only storage
  described under 0.5.0.

## [0.5.0] - 2026-10-11

### Added

* The experiment page pages its panels and charts, so its cost no longer grows with the experiment (#168: 886
  charts took about 30 s). It shows one page of the active tab's panels (default 10) with a pager, and a
  fuzzy filter on panel names (`vl` finds `val/loss`); inside a panel, one page of its charts (default 12), with
  a filter on the metric and artifact names they show. Only the charts on screen are fetched, on load and on
  every live-update poll. The page and filters are in the URL (`?panels_page=`, `?panels_q=`, `?charts=`), so a
  link reproduces what you were looking at, and a `?chart=` link lands on the page holding that chart. How many
  panels and charts per page is each viewer's own choice, kept in a `dlboard_page_sizes` cookie.
* A page left open across an upgrade offers a **Reload** prompt instead of failing every live-update poll: the
  server answers a callback request in an older shape with a `409` (not Dash's bare `500`), and the page
  shows the prompt once.
* A run's checkpoints are one list, not one chart per file. The file list takes a `key_prefix`
  (`checkpoints/`) in place of a `key` and shows every key under it, including ones logged later; Auto-generate
  and Suggest charts make exactly that for a directory of files, so a run that kept hundreds of checkpoints no
  longer gets hundreds of charts. The list is paged and sortable, and splits into `Latest`, `Best` and `Top-k`
  tabs from the tags `DLBoardLogger` records (any other file kind gets its own tab). A view with a
  `key_prefix` chart is not valid on an older server, so it can't be opened after a downgrade; the `height`
  setting no longer does anything (the list pages instead of scrolling) but is still accepted.

### Changed

* Only the active tab renders; the other tabs' panels are no longer fetched and drawn behind it. Dragging a
  panel or chart to reorder it now rebuilds the page of panels (or charts) being shown, rather than moving the
  nodes already on screen, so it can only be dropped among what is showing. Opening and closing a panel keeps
  the open state of the panels on other pages and tabs.
* Line charts render much faster on a panel with many charts: the server no longer copies and scans the whole
  panel's data once per chart (200 charts took 99 s of server time, now 4.6 s).
* Chart axes are readable: the value axis has room for its ticks beside the rotated title, ticks show at most
  four significant digits (no more `0.30000000000000004`), and a line chart's tooltip is titled with the whole
  metric name, which the axis title may shorten.
* The panel menu button is a sliders icon rather than three dots (which read as one more drag handle next to
  the grip), the header is padded off the panel's right border, and each button that rebuilds the panel area
  (create, rename, delete) spins until it finishes.
* `dlboard` now depends on `dlboard-client~=X.Y.0`, a client of its own minor version, instead of any
  version, so an environment holding both can no longer end up with a mismatched pair.
* `DataStore.fetch_artifacts` takes a `key_prefixes` keyword (default none) that also selects every artifact whose
  key starts with one of the prefixes, and `ChartType.hint_required_artifact_key_prefixes` lets a chart ask for
  them. A custom `DataStore` must accept the new keyword; a custom `ChartType` needs no change.

## [0.4.0] - 2026-10-09

### Added

* A run records how it ended and when. `DLBoardLogger.finalize` reports `"success"` as *finished* and
  `"failed"` as *failed* (`POST /api/v1/runs/<id>/finish`, a new path), stamped with the client's clock; the
  runs table gets a compact **Status** column (`running`, `3m 23s`, `2d 3h`, `✕ 1h 2m`) that sorts by
  duration and filters by status. A script that exits without finalizing the logger is recorded as *unknown*;
  a process killed outright never reports, so its run stays *running*. Two nullable columns are
  added to `Run` by migration `0002` (expand-only: run `dlboard` servers upgrade first, as always); an older
  server answers 404 to the new path and the client carries on.
* **Suggest charts** is a selection list, not one add button per key: a filter (by key or target panel), a
  By name / By panel sort, **Select all** and **Clear** for what the filter shows, and **Add selected (N)**,
  which adds everything ticked in a single page update (65 charts at once on the advanced example, instead of
  65 clicks and 65 rebuilds). The one-click add on each row is still there.
* Artifact `tags` (on `Image`, `Figure`, `File`, `Link` and anything implementing `AnyArtifact`) accept
  numbers and booleans as well as strings, and format them identically wherever they come from (floats to
  four significant digits). Callers no longer stringify, and so no longer disagree. Tags are still stored
  as text -- a numeric tag would be rejected by an older server, which the supported version skew allows.
* `DLBoardLogger` flattens nested hyperparameters, like Lightning's own loggers do: a pydantic model,
  a pydantic or stdlib dataclass, a `Namespace` or a plain dict, nested however deeply, is logged as
  `parent/child` keys instead of being rejected. Pydantic does the serializing, so a model's own
  aliases and field serializers apply. Lists are stored as a JSON string. See `flatten_hparams`.
* `DLBoardLogger(log_model=...)` uploads `ModelCheckpoint` checkpoints, like the MLflow and W&B
  loggers: `"all"` as each one is saved, `True` only the ones kept once training finishes. Backed by
  the new `dlboard.client.artifacts.file.File` artifact kind, which uploads a file in place and tags
  what it is (`FileKind.CHECKPOINT`).
* `examples/lightning_advanced.py`: a 25-epoch UNet on synthetic shapes with nested hyperparameters,
  Lightning's learning-rate, throughput and device-stats callbacks, checkpoints, and segmentation and
  bounding-box previews -- a deliberately heavy run for exercising the UI.

### Changed

* A checkpoint (anything logged as a `File`) now gets a **file list** with download links when charts are
  auto-generated or suggested, instead of an image chart full of broken images. It's the new built-in
  `files` chart type. Chart types now say which artifacts they can display (`ChartType.can_display_artifact`),
  and an artifact no chart type claims gets no generated chart at all.
* `Image` artifacts are encoded as lossless PNG by default instead of always as lossy JPEG, so masks,
  label images and overlays are no longer degraded on upload. Pass `format="jpg"` to keep the old,
  smaller files. Artifacts already stored are unchanged.
* A panel's header shows how many charts it holds, so you can tell how big it is before expanding it.

### Fixed

* `dlboard` and `dlboard-client` now import together when each is installed into its own directory on `sys.path`, as
  hermetic builds such as Bazel do. The client's regular `dlboard` package used to hide the server's half
  (`dlboard.serve`, `dlboard.plugins`), so depending on `dlboard` failed to import it. `dlboard/__init__.py` now
  extends its `__path__` with `pkgutil.extend_path`. Ordinary single-directory installs are unaffected.
* Creating charts with **Auto-generate** no longer makes the page wait for the first panel to draw when that panel
  is big. A panel opens by itself only if it holds 12 charts or fewer (otherwise the first small one does,
  or none); a big panel stays closed with its chart count on its header. On a run with 886 charts the
  default grouping now shows its panels in about 1.6 s instead of 5.8 s.
* Switching **Prefix**/**Suffix** in the Auto-generate charts dialog no longer freezes the page for seconds on an
  experiment with many metrics: the preview lists the first 20 panels and counts the rest (239 panels took
  the browser about 3 s to draw; it takes about 0.1 s now).
* Changing a Grid panel's charts-per-row no longer rebuilds every chart in the panel. The browser applies the
  new column count itself and the server only saves it, so a panel with over a hundred charts freezes the
  page for about a quarter of the time (0.8s instead of 3s per click, measured on 126 charts).
* A chart could show the wrong image when the browser had visited another dlboard database at the same
  address (a wiped or restored database, or a different `--sqlite-location`). Artifact URLs are keyed by
  id, ids start over in a new database, and the browser was told to cache them for a year; so artifact
  `5` of the old database was shown in place of artifact `5` of the new one. Artifact URLs now carry a
  version token (`?v=`) that changes with the artifact behind the id.
* A long metric name no longer spills its y-axis title out of the chart (over the chart header and the
  charts beside it): titles are shortened to fit, keeping the end of the name. How long a title may be is a
  setting of the line and bar charts (`max_axis_label_chars`, 36 by default).
* Metrics and artifacts logged by a later stage were never shipped when a script exited right after
  it -- notably everything `trainer.test` logs after `trainer.fit`. Lightning finalizes the logger
  after every stage, but only the first call flushed.

## [0.3.1] - 2026-10-08

* Reduce numpy requirement floor to 1.26 to expand compatibility.

## [0.3.0] - 2026-10-08

### Added

* `DLBoardLogger.log_figure(figure, key, step)`, a near drop-in for `mlflow.log_figure`: renders a
  matplotlib figure (a lossless PNG by default, or whatever supported extension the key ends in) and
  logs it as an image-series artifact. Backed by the new
  `dlboard.client.artifacts.figure.Figure` artifact kind.
* Run compare: a **Compare** button above the experiment's run table opens a modal that diffs up to
  five runs' hyperparameters and latest metric values, as differences only or every key, with a
  search box over keys and values. Differing cells are highlighted against the first run picked.
  The picked runs, mode and search are kept in the URL, so copying the address (or the modal's link
  button) shares exactly what you're looking at.
* `DLBOARD_SESSION_CLOCK_SKEW_SECONDS` (default `120`): how far ahead of a replica's own clock a
  session cookie may be dated and still be accepted. See [docs/configuration.md](docs/configuration.md).

### Fixed

* People are no longer randomly signed out, or bounced with a 400 from the login form, when a
  session cookie was signed by a replica (or a clock) a moment ahead of the one reading it. Flask
  rejected such a cookie outright and treated the browser as signed out; under several replicas, or
  across a clock step, that hit perfectly good sessions. Only the lower bound is relaxed -- a cookie
  older than the session lifetime still expires as before, and revoking a session is unaffected.

## [0.2.1] - 2026-10-07

* Lower version requirements to enable broader install base
* Fix Dash in tab title
* Adjust readme

## [0.2.0] - 2026-10-07

* Change dlboard to install the full fat server+client and dlboard-client to install only the pytorch lightning logger to make it more straightforward to install for tensorboard-like use cases.

## [0.1.3] - 2026-10-07

### Added

- First public release candidate
  * Builtin charts: bar chart, line chart, table, image series
  * Custom plugins for extensible charts, backend functionality, logging, theme, and more, all powered by Dash/Plotly
  * Asynchronous hyperparameter and metric logging
  * Pytorch lightning logger support out of the box
  * Local serve/client experience like tensorboard
  * Production ready, s3 and postgres backing file and data stores built in
