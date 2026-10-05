# dltrack

A `pytorch_lightning`-compatible logger client for [dltrack](https://github.com/dlangerm/dltrack), a
free, self-hosted ML experiment-tracking server. This package ships metrics, hyperparameters, and
artifacts from a training script to a running dltrack server over a REST API.

```python
from dltrack.client.dltrack_logger import DLTrackLogger

logger = DLTrackLogger.from_names("my-project", "my-experiment", "a description")
trainer = pl.Trainer(logger=logger)
```

You'll also need a dltrack server to point it at — either `pip install dltrack[server]` for a
single-machine setup, or a shared deployment your team already runs. See the
[project README](https://github.com/dlangerm/dltrack#readme) for the full picture, and
[docs/client.md](https://github.com/dlangerm/dltrack/blob/main/docs/client.md) for logger setup,
authentication, and logging custom artifact types.
