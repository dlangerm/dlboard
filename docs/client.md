# Client & logging

## What is this

`dlboard.client.dlboard_logger.DLBoardLogger` (`dlboard/client/dlboard_logger.py`) is a
`pytorch_lightning.loggers.Logger` — drop it into a `Trainer` like any other PL logger. Under the
hood it talks to a running dlboard server over REST via `BasicDlboardAPI`
(`dlboard/client/_rest_api.py`), and spawns two daemon subprocesses that batch
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
up automatically. See `examples/lightning_quickstart.py` for a complete, runnable example (MNIST MLP), and
`examples/lightning_advanced.py` for a heavier one (nested hyperparameters, checkpoints, mask and box
previews, Lightning's monitoring callbacks) that doubles as a stress test for the UI.

**Hyperparameters.** `save_hyperparameters()` (or `logger.log_hyperparams(...)`) takes anything
nested: pydantic models, pydantic and stdlib dataclasses, a `Namespace`, plain dicts and ordinary
init args. They're flattened to `parent/child` keys (`optim/schedule/warmup_steps`), which the
experiment page's hyperparameter columns and run compare group on the same `/`. Pydantic serializes
them, so a model's aliases and field serializers apply; a list is stored as one JSON string, and
scalars are stored exactly as given.

**Checkpoints.** `DLBoardLogger(..., log_model="all")` uploads every checkpoint a `ModelCheckpoint`
saves, even one `save_top_k` deletes moments later (each is copied before it's queued). `log_model=True`
uploads only the ones still kept, when training finishes. Each is a `File` (below) of kind `checkpoint`,
keyed `checkpoints/<file name>`, tagged with its score and Lightning's `latest`/`best`/`best_k`, and
stamped with the global step its callback saved at (with `log_model=True`, the step of that callback's last
save). Any number of `ModelCheckpoint` callbacks work. On the experiment page they are one list, split into
`Latest`, `Best` and `Top-k` tabs by those tags, paged and sortable by step and score.

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

**How a run ends.** Lightning finalizes the logger when a stage finishes: `"success"` records the run as
*finished* and `"failed"` as *failed*, with the time it ended (stamped by your machine, like the run's start,
 so the duration never mixes two clocks). The runs table shows it as "running", "3m 23s", "2d 3h" or "✕ 1h 2m" next to
the run. A later stage (`trainer.test` after `fit`) moves the end time later. A script that exits without Lightning
finalizing the logger (an exception outside the trainer, say) is recorded as *unknown* -- ended, but not known how.
A process that is killed outright (`kill -9`, the OOM killer) can't say anything, so its run stays *running*. A
server that predates this simply doesn't record any of it.

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

**Tracing.** To see where a slow batch spent its time, install `dlboard-client[otel]` and set
`DLBOARD_TELEMETRY_TRACES=true` (with the standard `OTEL_EXPORTER_OTLP_ENDPOINT`) in your training environment. Each
batch the shipping processes send becomes one trace, and a server with tracing on (see [telemetry](telemetry.md#traces))
continues it through its database and artifact storage. It asks little of the rest of your environment: only the
standard OpenTelemetry SDK and OTLP exporter, release 1.16 or later with no upper bound, and nothing is patched or made
the global tracer in your training process. A collector that is down costs spans, never a training step.

**Histograms.** `logger.log_histograms({"grad_norm/layer1": tensor}, step=global_step)` logs the distribution
of any array, tensor or list of numbers at a step -- per-layer gradient norms, weights, activations, per-sample
losses -- instead of averaging it into one scalar. The values are binned **in your training process, at the call**,
in one pass, and only the bucket edges and counts are queued and sent: the server never sees (or computes
over) the raw values, so a huge tensor or a fine binning slows down only the script that chose it. A histogram
has at most 1024 buckets (`DLBOARD_MAX_HISTOGRAM_BUCKETS`, set on the server and, for a finer binning, in your
training environment too); a binning that needs more raises at the call rather than being rejected later.

How values are bucketed is the `binning=` argument:

- `ExponentialBinning(buckets_per_decade=10, zero_threshold=1e-12)` (the default) has a fixed number of buckets
  per power of ten, mirrored for negative values, with one bucket around zero. It suits values spread over many
  orders of magnitude. Its edges depend only on those two settings, never on the data, so every step and run lines
  up bucket for bucket, and a stretch with no values costs one empty bucket.
- `ExplicitBinning(edges=[...])` is buckets exactly where you say. Values outside the edges are not dropped: an
  extra bucket reaching the smallest or largest value seen holds them.

NaN and infinite values are counted aside, not binned. Pass a `dlboard.models.Histogram` you already binned
(its `edges` and `counts`) instead of values to send it as it is. Histograms ride the same queue and
shipping process as metrics, so the backpressure notes below apply to them too. A server older than the
version that added histograms ignores them, so upgrade the server first.

**Logging artifacts.** Call `logger.log_artifact([...])` with anything implementing `AnyArtifact`
(`dlboard/models/_artifact.py`): a `key`, `tags`, a `step`, and a
`to_artifact(local_temp, run_id, experiment_id) -> tuple[NewArtifact, Path | AnyUrl]` method.
Returning a `Path` (written under `local_temp`) uploads that file; returning an `AnyUrl` instead
registers it as a *link* — no bytes move, the server just checks the ref is one its
`ArtifactStore` can actually serve (see [Storage](plugins/storage.md)) and records it. `tags` are shown as a
caption next to the artifact and take strings, numbers or booleans: numbers are formatted the same way
everywhere (floats to four significant digits, `0.1410`), so log `tags={"iou": iou}` rather than
formatting it yourself. They are sent and stored as the numbers they are. A number with a unit is a
`Quantity`: `tags={"took": Quantity(value=5400, unit=TimeUnit.SECONDS)}` says what the number was measured
in (time: `ns` to `d`; size: `B` to `TB`, decimal), and nothing about how it should look. The chart decides
that: by default the largest unit that fits (`1.500 h`, `2.500 GB`), or the `tag_time_unit` /
`tag_size_unit` it is set to (`5,400,000 ms`), with `tag_precision` significant digits (default 4); the chart's
`tag_display` table (in its editor, a row per tag name) overrides those for a particular tag. Plain
numbers are written out with separators (`12,346`) rather than in exponent form.
There are five built-in kinds:

- `dlboard.client.artifacts.image.Image` — uploads. Wraps a `torch.Tensor`/`np.ndarray`
  (validated CHW `uint8` via `dlbype`), used like:

  ```python
  from dlboard.client.artifacts import image

  logger.log_artifact([image.Image(key="sample", image=tensor, step=global_step)])
  ```

  It is encoded as a lossless PNG by default, so masks and label images stay pixel-exact. Pass
  `format="jpg"` for a much smaller (lossy) file when it is a large photograph. To annotate a
  photograph, log an `Overlay` beside it rather than drawing into its pixels.

- `dlboard.client.artifacts.overlay.Overlay` — uploads the shapes drawn over a picture: `Box`es
  (`x`, `y`, `width`, `height`, from the top-left corner), `Polyline`s and `Polygon`s (`points`; a
  polygon is a polyline that is closed), each with an optional `label` and `color` (`"#rrggbb"`).
  Log it at the same step as its picture. The picture can stay a small JPEG, and the shapes stay
  sharp at any size, because the image chart draws them over it in the browser:

  ```python
  from dlboard.client.artifacts import image, overlay
  from dlboard.client.artifacts.overlay import Box

  logger.log_artifact(
      [
          image.Image(key="sample", image=tensor, step=global_step, format="jpg"),
          overlay.Overlay.over(
              "sample",
              name="detections",
              shapes=[Box(x=0.2, y=0.15, width=0.4, height=0.3, label="cat", color="#00c853")],
              step=global_step,
          ),
      ]
  )
  ```

  `Overlay.over` logs it under the image's key plus `_$overlay` and its `name` (`sample_$overlay/detections`), which is where
  auto-generate looks: the chart it makes for `sample` draws that artifact over it, and gives it no
  chart of its own. An image logged under that key (a segmentation mask with transparent
  background, say) is drawn over the picture the same way, and `models.overlay_key("sample")`
  builds the key for it. Under any other key (`Overlay(key=...)`), add it to the chart's
  `extra_layers` setting yourself.

  An image can have several overlays (predicted and ground-truth boxes, say). Only one artifact
  per key and step is shown, so give each its own `name`, which goes after the suffix
  (`Overlay.over("sample", name="truth", ...)` logs `sample_$overlay/truth`). A name is what the chart's legend calls the layer,
  so choose one that says what it is (`truth`, `prediction`, `detections`): an overlay with no name is called "overlay". Auto-generate draws
  all of them, bottom layer first in key order, including ones logged later. The logger warns when one call logs an overlay
  without its image, or a key twice at the same step, and the client warns once when an artifact
  that is not an overlay is logged under a key that holds the suffix.

  Shapes are stored as fractions of the picture's size, from 0 to 1, so they need no knowledge of
  the picture's own size to be drawn: divide a pixel coordinate by the picture's width (for `x`) or
  height (for `y`). The origin is the top-left corner and `y` grows downward, the way image
  libraries index pixels. A line's points are `Point(x, y)` tuples. An overlay takes at most 1000
  shapes, and a label up to 1000 characters. A picture with nothing logged at its step is shown
  alone. An older server stores the overlay without drawing it, so upgrade the server to see the
  shapes.

  A segmentation mask is a shape too. `dlboard.client.artifacts.rle.run_length_encode` turns a
  per-pixel class array (`*batch classes height width` of integer IDs, the last two being the
  picture's height and width) into an `ImageMask`, run-length encoded so it stays exact and small:

  ```python
  from dlboard.client.artifacts.rle import run_length_encode

  mask = run_length_encode(class_ids[None], colors={1: "#e63c3c"}, classnames={1: "cat"})
  overlay.Overlay.over("sample", name="truth", shapes=[mask], step=global_step)
  ```

  A class with a color in `colors` is drawn in it at the mask's `opacity` (0.5 by default, so the
  picture shows through); a class with none is left see-through, which keeps a background class
  clear. Leave `colors` out to draw ID 0 clear and every other ID in a color of a built-in palette.

- `dlboard.client.artifacts.figure.Figure` — uploads a matplotlib figure as an image. You
  rarely build one yourself: `logger.log_figure` mirrors `mlflow.log_figure`, with the one
  difference that dlboard needs a `step`:

  ```python
  # mlflow: mlflow.log_figure(fig, "loss_curve.png")
  logger.log_figure(fig, "loss_curve.png", step=global_step)  # same name works as-is
  logger.log_figure(fig, "loss_curve", step=global_step)  # or leave the extension off
  ```

  The format is a lossless PNG by default. If the key ends in another supported extension
  (`.png`, `.jpg`, `.jpeg`, `.svg`) that format is used instead, and a `format` in `save_kwargs`
  overrides both (an unsupported one is an error). Any other extension just falls back to PNG. The
  key itself is used exactly as given. The figure is rendered right at the call, so it's safe to
  `plt.close(fig)` straight after. `tags=` is optional, and `save_kwargs=` goes to `fig.savefig`
  (`dpi`, `bbox_inches`, ...). Any object with a matplotlib-style `savefig(fname, **kwargs)`
  works, and dlboard doesn't depend on matplotlib itself.

- `dlboard.client.artifacts.file.File` — uploads a file that already exists on disk, as-is, tagged with a
  `FileKind` saying what it is (`FileKind.CHECKPOINT`). `log_model` builds these for you; the
  file must stay put until the logger has flushed:

  ```python
  from dlboard.client.artifacts.file import File
  from dlboard.models import FileKind

  logger.log_artifact([File(key="weights", path=path, kind=FileKind.CHECKPOINT, step=global_step)])
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
