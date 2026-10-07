# Changelog

All notable changes to dlboard are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows the tiers described in
[docs/compatibility.md](docs/compatibility.md).

<!--
To cut a release: rename this section to `## [X.Y.Z] - YYYY-MM-DD`, start a fresh empty
`## [X.Y.Z]` above it, bump `dlboard/_version.py`, and tag `vX.Y.Z` -- the release workflow
pulls that section's body into the GitHub release notes and publishes both distributions from it.
-->

## [0.1.0] - 2026-10-06

### Added

- First public release candidate
  * Builtin charts: bar chart, line chart, table, image series
  * Custom plugins for extensible charts, backend functionality, logging, theme, and more, all powered by Dash/Plotly
  * Asynchronous hyperparameter and metric logging
  * Pytorch lightning logger support out of the box
  * Local serve/client experience like tensorboard
  * Production ready, s3 and postgres backing file and data stores built in
