"""Dltrack API class."""

import requests

from dltrack import models


class DltrackAPI:
    """API class."""

    def __init__(self, base_url: str = "http://localhost:8050") -> None:
        """Initialize the API class."""
        self.base_url = base_url

    def create_project(self, new_project: models.NewProject) -> models.Project:
        """Create a new project."""
        res = requests.post(
            f"{self.base_url}/create/{models.Project.__name__}",
            json=new_project.model_dump(mode="json"),
        )
        res.raise_for_status()
        return models.Project.model_validate(res.json())

    def create_experiment(self, new_experiment: models.NewExperiment) -> models.Experiment:
        """Create a new experiment."""
        res = requests.post(
            f"{self.base_url}/create/{models.Experiment.__name__}",
            json=new_experiment.model_dump(mode="json"),
        )
        res.raise_for_status()
        return models.Experiment.model_validate(res.json())

    def create_run(self, run: models.NewRun) -> models.Run:
        """Initialize a new run."""
        res = requests.post(
            f"{self.base_url}/create/{models.Run.__name__}",
            json=run.model_dump(mode="json"),
        )
        res.raise_for_status()
        return models.Run.model_validate(res.json())

    def log_hyperparams(self, hyperparams: models.NewHyperParams) -> None:
        """Log hyperparameters."""
        res = requests.post(
            f"{self.base_url}/log-hyperparams",
            json=hyperparams.model_dump(mode="json"),
        )
        res.raise_for_status()

    def log_metric_batch(self, metrics: list[models.LoggedMetrics]) -> None:
        """Log a batch of metrics."""
        res = requests.post(
            f"{self.base_url}/log-batch",
            json=[m.model_dump(mode="json") for m in metrics],
        )
        res.raise_for_status()
