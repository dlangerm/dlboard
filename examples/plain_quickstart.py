"""
A training loop with no framework at all, logged to dlboard: numpy linear regression by gradient descent.

Needs only `dlboard-client` (no Lightning, no torch). Start a server (`dlboard serve local`), then run this; a run
appears in the `quickstart` project's `plain` experiment, with its loss, its weights' spread as a histogram every
few steps, and how it ended.
"""

import numpy as np

from dlboard.client import run as dlboard

STEPS = 500
FEATURES = 32


def main() -> None:
    """Fit `true_weights` from noisy samples, logging the loss every step and the weights every 25."""
    rng = np.random.default_rng(0)
    true_weights = rng.normal(size=FEATURES)
    inputs = rng.normal(size=(1024, FEATURES))
    targets = inputs @ true_weights + rng.normal(scale=0.1, size=1024)
    config = {"lr": 0.05, "batch_size": 64, "features": FEATURES}

    with dlboard.init("quickstart", "plain", hparams=config) as run:  # server_url= for a server elsewhere
        weights = np.zeros(FEATURES)
        for step in range(STEPS):
            batch = rng.choice(len(inputs), size=config["batch_size"], replace=False)
            error = inputs[batch] @ weights - targets[batch]
            weights -= config["lr"] * inputs[batch].T @ error / len(batch)
            run.log_metrics({"train/loss": float(np.mean(error**2))}, step=step)
            if step % 25 == 0:
                run.log_metrics({"weight_error": float(np.linalg.norm(weights - true_weights))}, step=step)
                run.log_histograms({"weights": weights}, step=step)


if __name__ == "__main__":  # the shipping processes re-import this module: see docs/client.md
    main()
