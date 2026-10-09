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
