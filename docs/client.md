# Client & logging

## What is this

`dltrack.client.DLTrackLogger` (`dltrack/client/dltrack_logger.py`) is a
`pytorch_lightning.loggers.Logger` — drop it into a `Trainer` like any other PL logger. Under the
hood it talks to a running dltrack server over REST via `BasicDltrackAPI`
(`dltrack/plugins/backend/basic_rest_backend.py`), and spawns two daemon subprocesses that batch
metrics and artifacts off multiprocessing queues and flush them on an interval, so training is
never blocked on network I/O.

## When you'd need this

Every training script that wants runs to show up in dltrack uses this. This page is also where to
look if you want to log a new *kind* of artifact (images are the only built-in kind today) — see
below.

## How it works

```python
from dltrack.client import DLTrackLogger

logger = DLTrackLogger(project_id=project.id, server_url="http://localhost:8050")
trainer = pl.Trainer(logger=logger)
```

If you don't already have an experiment, omit `experiment_id` and one is created for you. Metrics
logged via PL's normal `self.log(...)` and hyperparameters via `save_hyperparameters()` are picked
up automatically. See `train.py` at the repo root for a complete, runnable example (MNIST MLP).

**Logging artifacts.** Call `logger.log_artifact([...])` with anything implementing `AnyArtifact`
(`dltrack/models/_artifact.py`): a `key`, `tags`, a `step`, and a `to_artifact(local_temp, run_id,
experiment_id)` method that writes the artifact to a local temp path and returns the `NewArtifact`
metadata plus that path for upload. `dltrack.plugins.artifacts.image.Image` is the only built-in
kind — wraps a `torch.Tensor`/`np.ndarray` (validated CHW `uint8` via `dltype`), used like:

```python
from dltrack.plugins.artifacts import image

logger.log_artifact([image.Image(key="sample", image=tensor, step=global_step)])
```

**Adding a new artifact kind.** Note this is a *client-side* protocol, not a `PluginProtocol`
plugin — there's no `plug()`, and it's never passed into `app()`. Implement `AnyArtifact`
(the four members above) for whatever you're logging — a plot, a model checkpoint, a text blob —
and pass instances of it to `log_artifact` the same way. The server stores it as an opaque blob
plus whatever metadata `to_artifact` put in the `NewArtifact`; if you also want a dedicated way to
*render* that kind of artifact, pair it with a [chart plugin](plugins/charts.md) — see
`image_series.py` for how the built-in image chart type reads back what `Image.to_artifact` wrote.
