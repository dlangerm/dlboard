# Changelog

All notable changes to dltrack are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows the tiers described in
[docs/compatibility.md](docs/compatibility.md).

## [Unreleased]

### Added

- First public release candidate: frozen/tested migrations, a versioned REST API, hardened session
  and artifact-download security, a client that never blocks training on a down or slow server, and
  a `uv`-based packaging and release pipeline.

<!--
To cut a release: rename this section to `## [X.Y.Z] - YYYY-MM-DD`, start a fresh empty
`## [Unreleased]` above it, bump `dltrack/_version.py`, and tag `vX.Y.Z` -- the release workflow
pulls that section's body into the GitHub release notes and publishes both distributions from it.
-->
