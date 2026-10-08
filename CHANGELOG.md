# Changelog

All notable changes to dlboard are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows the tiers described in
[docs/compatibility.md](docs/compatibility.md).

<!--
To cut a release: rename this section to `## [X.Y.Z] - YYYY-MM-DD`, start a fresh empty
`## [X.Y.Z]` above it, bump `dlboard/_version.py`, and tag `vX.Y.Z` -- the release workflow
pulls that section's body into the GitHub release notes and publishes both distributions from it.
-->

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
