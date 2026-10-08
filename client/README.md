# dlboard

A `pytorch_lightning`-compatible logger client for [dlboard](https://github.com/dlangerm/dlboard), a
free, self-hosted ML experiment-tracking server. This package ships metrics, hyperparameters, and
artifacts from a training script to a running dlboard server over a REST API.

```bash
pip install "dlboard-client[lightning]"
```

```python
from dlboard.client.dlboard_logger import DLBoardLogger

logger = DLBoardLogger.from_names("my-project", "my-experiment", "a description")
trainer = pl.Trainer(logger=logger)
```

The `lightning` extra is what the logger needs; without it you still get the REST client and data
models. You'll also need a dlboard server to point it at — either `pip install dlboard` (the separate
server package, which includes this client) and `dlboard serve local` for a single-machine setup, or a
shared deployment your team already runs. See the
[project README](https://github.com/dlangerm/dlboard#readme) for the full picture, and
[docs/client.md](https://github.com/dlangerm/dlboard/blob/main/docs/client.md) for logger setup,
authentication, and logging custom artifact types.
