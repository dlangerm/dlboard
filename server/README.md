# dltrack-server

The server half of [dltrack](https://github.com/dlangerm/dltrack): a free, self-hosted Dash web app
for browsing ML training runs, plus the `dltrack` CLI that runs it. Installing this (it depends on
the `dltrack` client package for the shared data models) gives you a `dltrack` command, the same
idea as `tensorboard`:

```bash
dltrack serve local     # anonymous, single-user, sqlite + local disk -- sane defaults, no setup
```

Storage, auth, chart types, and themes are all swappable plugins — see
[docs/plugins/overview.md](https://github.com/dlangerm/dltrack/blob/main/docs/plugins/overview.md)
for composing your own deployment with `dltrack serve custom`. See the
[project README](https://github.com/dlangerm/dltrack#readme) for the full picture, including
screenshots of the app itself.
