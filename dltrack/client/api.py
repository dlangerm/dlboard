"""Dltrack API class."""

import requests

from dltrack.models import Experiment, NewExperiment


class DltrackAPI:
    """API class."""

    BASE_URL = "http://localhost:8050"

    @classmethod
    def create_experiment(cls, new_experiment: NewExperiment) -> Experiment:
        """Create a new experiment."""
        res = requests.post(
            f"{cls.BASE_URL}/create/{Experiment.__name__}", json=new_experiment.model_dump(mode="json")
        )
        res.raise_for_status()
        return Experiment.model_validate(res.json())
