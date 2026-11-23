# dlboard

The training-side client for [dlboard](https://github.com/dlangerm/dlboard), a free, self-hosted ML experiment-tracking server.
This package ships metrics, hyperparameters, and artifacts from a training script -- any training loop, or a PyTorch Lightning `Trainer` -- to a running dlboard server over a REST API.

```bash
pip install dlboard-client
```

```python
from dlboard.client import run as dlboard

with dlboard.init("my-project", "my-experiment", hparams=config) as run:
    for step, batch in enumerate(loader):
        run.log_metrics({"train/loss": train_step(batch)}, step=step)
```

With PyTorch Lightning, install the `lightning` extra and use the logger instead:

```bash
pip install "dlboard-client[lightning]"
```

```python
from dlboard.client.dlboard_logger import DLBoardLogger

logger = DLBoardLogger.from_names("my-project", "my-experiment", "a description")
trainer = pl.Trainer(logger=logger)
```

You'll also need a dlboard server to point it at — either `pip install dlboard` (the separate server package, which includes this client) and `dlboard serve local` for a single-machine setup, or a shared deployment your team already runs.
See the [project README](https://github.com/dlangerm/dlboard#readme) for the full picture, and [docs/client.md](https://github.com/dlangerm/dlboard/blob/main/docs/client.md) for setup, authentication, and logging custom artifact types.
