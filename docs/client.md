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
(`dltrack/models/_artifact.py`): a `key`, `tags`, a `step`, and a
`to_artifact(local_temp, run_id, experiment_id) -> tuple[NewArtifact, Path | AnyUrl]` method.
Returning a `Path` (written under `local_temp`) uploads that file; returning an `AnyUrl` instead
registers it as a *link* — no bytes move, the server just checks the ref is one its
`ArtifactStore` can actually serve (see [Storage](plugins/storage.md)) and records it. There are
two built-in kinds:

- `dltrack.plugins.artifacts.image.Image` — uploads. Wraps a `torch.Tensor`/`np.ndarray`
  (validated CHW `uint8` via `dltype`), used like:

  ```python
  from dltrack.plugins.artifacts import image

  logger.log_artifact([image.Image(key="sample", image=tensor, step=global_step)])
  ```

- `dltrack.plugins.artifacts.link.Link` — links. For a blob a training job already wrote
  somewhere dltrack's `ArtifactStore` can serve (e.g. the same S3 bucket), without shipping the
  bytes through dltrack a second time:

  ```python
  from pydantic import AnyUrl
  from dltrack.plugins.artifacts import link

  logger.log_artifact([link.Link(key="checkpoint", ref=AnyUrl("s3://my-bucket/ckpt.pt"), step=global_step)])
  ```

**Adding a new artifact kind.** Note this is a *client-side* protocol, not a `PluginProtocol`
plugin — there's no `plug()`, and it's never passed into `app()`. Implement `AnyArtifact`
(the four members above) for whatever you're logging — a plot, a model checkpoint, a text blob —
and pass instances of it to `log_artifact` the same way. The server stores it as an opaque blob
plus whatever metadata `to_artifact` put in the `NewArtifact`; if you also want a dedicated way to
*render* that kind of artifact, pair it with a [chart plugin](plugins/charts.md) — see
`image_series.py` for how the built-in image chart type reads back what `Image.to_artifact` wrote.
