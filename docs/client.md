# Client & logging

## What is this

`dlboard.client.DLBoardLogger` (`dlboard/client/dlboard_logger.py`) is a
`pytorch_lightning.loggers.Logger` — drop it into a `Trainer` like any other PL logger. Under the
hood it talks to a running dlboard server over REST via `BasicDlboardAPI`
(`dlboard/plugins/backend/basic_rest_backend.py`), and spawns two daemon subprocesses that batch
metrics and artifacts off multiprocessing queues and flush them on an interval, so training is
never blocked on network I/O.

## When you'd need this

Every training script that wants runs to show up in dlboard uses this. This page is also where to
look if you want to log a new *kind* of artifact (images are the only built-in kind today) — see
below.

## How it works

```python
from dlboard.client.dlboard_logger import DLBoardLogger

logger = DLBoardLogger(project_id=project.id, server_url="http://localhost:8050")
trainer = pl.Trainer(logger=logger)
```

If you don't already have an experiment, omit `experiment_id` and one is created for you. Metrics
logged via PL's normal `self.log(...)` and hyperparameters via `save_hyperparameters()` are picked
up automatically. See `examples/lightning_quickstart.py` for a complete, runnable example (MNIST MLP).

**Authenticating.** If the server signs people in (see [Auth](plugins/auth.md)), the script has to
sign in too:

- Create an API token on your Account page.
- Set it as `DLBOARD_API_KEY` wherever the script runs, or pass `api_key=` to `BasicDlboardAPI`.

Before it starts, the logger checks the key with the server's `/api/v1/whoami` and fails right away if
it's missing or wrong. Without that check, bad credentials would only show up later, as metrics
that were never stored. `dlboard serve local` needs no key.

**Guard your script's entry point.** The two shipping processes are started with `multiprocessing`'s
`spawn` method, which re-imports your training script's `__main__` module from scratch in each one.
A script that builds the logger (or starts training) at module level, with no
`if __name__ == "__main__":` guard, re-runs that code in every shipping process too — usually
visible as training appearing to restart, or the logger itself being constructed more than once.
This is the same requirement Python's own `multiprocessing` docs describe for any spawn-based code,
not something specific to dlboard.

**Multi-GPU (DDP).** Every rank builds its own logger and Lightning calls all of them, so only
global rank 0 logs: a non-zero rank makes no server calls, starts no shipping processes and drops
whatever it's asked to log, which means one run per job rather than one per GPU. To log into a run
that already exists instead of creating one — resuming it, or sharing it between processes you
launch separately — pass `run_id=` or set `DLBOARD_RUN_ID`; the logger checks it exists up front
and uses its experiment. `logger.run_id` is how you read the id of a run the logger created, to
hand on to such a process.

**If the server (or your network) is down.** Logging never blocks training: `log_metrics`/
`log_artifact` queue onto a bounded buffer the shipping processes drain in the background, retrying
a failure with exponential backoff rather than hammering a server that's still down. If the buffer
fills faster than it can drain, the oldest-queued item is dropped (not blocked on), with a
rate-limited warning while it keeps happening and a final count at `finalize()`. Size the buffers
with `DLBoardLoggerSettings` (`metrics_q_size`, `artifact_q_size`, ...) if your logging rate is high
enough that this matters; `DLBOARD_CONNECT_TIMEOUT_S`/`DLBOARD_READ_TIMEOUT_S` (defaults 10s/60s)
cap how long any one request waits before it's treated as failed.

**Logging artifacts.** Call `logger.log_artifact([...])` with anything implementing `AnyArtifact`
(`dlboard/models/_artifact.py`): a `key`, `tags`, a `step`, and a
`to_artifact(local_temp, run_id, experiment_id) -> tuple[NewArtifact, Path | AnyUrl]` method.
Returning a `Path` (written under `local_temp`) uploads that file; returning an `AnyUrl` instead
registers it as a *link* — no bytes move, the server just checks the ref is one its
`ArtifactStore` can actually serve (see [Storage](plugins/storage.md)) and records it. There are
two built-in kinds:

- `dlboard.client.artifacts.image.Image` — uploads. Wraps a `torch.Tensor`/`np.ndarray`
  (validated CHW `uint8` via `dlbype`), used like:

  ```python
  from dlboard.client.artifacts import image

  logger.log_artifact([image.Image(key="sample", image=tensor, step=global_step)])
  ```

- `dlboard.client.artifacts.link.Link` — links. For a blob a training job already wrote
  somewhere dlboard's `ArtifactStore` can serve (e.g. the same S3 bucket), without shipping the
  bytes through dlboard a second time:

  ```python
  from pydantic import AnyUrl
  from dlboard.client.artifacts import link

  logger.log_artifact([link.Link(key="checkpoint", ref=AnyUrl("s3://my-bucket/ckpt.pt"), step=global_step)])
  ```

**Adding a new artifact kind.** Note this is a *client-side* protocol, not a `PluginProtocol`
plugin — there's no `plug()`, and it's never passed into `app()`. Implement `AnyArtifact`
(the four members above) for whatever you're logging — a plot, a model checkpoint, a text blob —
and pass instances of it to `log_artifact` the same way. The server stores it as an opaque blob
plus whatever metadata `to_artifact` put in the `NewArtifact`; if you also want a dedicated way to
*render* that kind of artifact, pair it with a [chart plugin](plugins/charts.md) — see
`image_series.py` for how the built-in image chart type reads back what `Image.to_artifact` wrote.
