# pyright: reportPrivateUsage=false
"""Tests for `DLTrackLogger.from_names`: looks up/creates by name, then defers to `__init__`.

`__init__` itself spawns real background processes and hits the network, so it's stubbed out here
-- these tests only cover the name-lookup wiring `from_names` adds on top of it.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from dltrack import models
from dltrack.client import dltrack_logger
from dltrack.client.dltrack_logger import DLTrackLogger, DLTrackLoggerSettings, warn_if_startup_was_slow


class _FakeAPI:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_or_create_project(self, name: str, description: str = "") -> models.Project:
        self.calls.append(("get_or_create_project", (name,), {"description": description}))
        return models.Project(id=1, name=name, description=description)

    def get_or_create_experiment(
        self, project_id: int, name: str = "default", source: models.ExperimentSource | None = None
    ) -> models.Experiment:
        self.calls.append(("get_or_create_experiment", (project_id,), {"name": name, "source": source}))
        return models.Experiment(id=2, project_id=project_id, name=name, source=source)


def _stub_api(monkeypatch: pytest.MonkeyPatch, fake_api: _FakeAPI) -> None:
    """Replace `BasicDltrackAPI` in `dltrack_logger` with a factory that always returns `fake_api`."""

    def _factory(base_url: str) -> _FakeAPI:
        del base_url
        return fake_api

    monkeypatch.setattr(dltrack_logger, "BasicDltrackAPI", _factory)


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace `DLTrackLogger.__init__` with a spy that just records its kwargs."""
    init_kwargs: dict[str, Any] = {}

    def _fake_init(self: DLTrackLogger, **kwargs: Any) -> None:  # noqa: ANN401
        init_kwargs.update(kwargs)

    monkeypatch.setattr(DLTrackLogger, "__init__", _fake_init)
    return init_kwargs


def test_from_names_looks_up_project_and_experiment_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    init_kwargs = _stub_init(monkeypatch)

    DLTrackLogger.from_names("proj", experiment_name="exp", project_description="desc", server_url="http://x")

    assert fake_api.calls == [
        ("get_or_create_project", ("proj",), {"description": "desc"}),
        (
            "get_or_create_experiment",
            (1,),
            {"name": "exp", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
        ),
    ]
    assert init_kwargs["project_id"] == 1
    assert init_kwargs["experiment_id"] == 2
    assert init_kwargs["server_url"] == "http://x"


def test_from_names_defaults_the_experiment_name_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    _stub_init(monkeypatch)

    DLTrackLogger.from_names("proj")

    assert fake_api.calls[1] == (
        "get_or_create_experiment",
        (1,),
        {"name": "default", "source": models.ExperimentSource.PYTORCH_LIGHTNING},
    )


def test_from_names_passes_settings_through(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_api = _FakeAPI("http://x")
    _stub_api(monkeypatch, fake_api)
    init_kwargs = _stub_init(monkeypatch)
    settings = DLTrackLoggerSettings(metrics_q_size=5)

    DLTrackLogger.from_names("proj", settings=settings)

    assert init_kwargs["settings"] is settings


def test_warn_if_startup_was_slow_warns_past_the_threshold() -> None:
    with pytest.warns(UserWarning, match="took 3.0s to start up"):
        warn_if_startup_was_slow(3.0)


def test_warn_if_startup_was_slow_is_silent_within_the_threshold() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        warn_if_startup_was_slow(0.1)
