# dlboard

The full [dlboard](https://github.com/dlangerm/dlboard) installation: a free, self-hosted Dash web app
for browsing ML training runs, plus the `dlboard` CLI that runs it. Installing this (it depends on
the `dlboard-client` package for the shared data models) gives you a `dlboard` command, the same
idea as `tensorboard`:

```bash
dlboard serve local     # anonymous, single-user, sqlite + local disk -- sane defaults, no setup
```

Storage, auth, chart types, and themes are all swappable plugins — see
[docs/plugins/overview.md](https://github.com/dlangerm/dlboard/blob/main/docs/plugins/overview.md)
for composing your own deployment with `dlboard serve custom`. See the
[project README](https://github.com/dlangerm/dlboard#readme) for the full picture, including
screenshots of the app itself.
