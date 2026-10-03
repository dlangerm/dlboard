# Security Policy

## Reporting a vulnerability

Please don't open a public issue for a security vulnerability.

Instead, use GitHub's private reporting for this repo: go to the
[Security tab](https://github.com/dlangerm/dltrack/security) and click "Report a vulnerability", or
go directly to the
[new advisory form](https://github.com/dlangerm/dltrack/security/advisories/new).

If you'd rather not use GitHub for the report, email dev.onyxzerosoftware@gmail.com instead.

Please include:

- What the vulnerability is and its likely impact (e.g. which auth provider or storage plugin it
  affects).
- Steps to reproduce it, or a minimal proof of concept.
- The dltrack version (or commit) you tested against.

## Supported versions

dltrack doesn't yet have a stable release line to backport fixes into -- until it does, only the
latest release and `main` are supported.
