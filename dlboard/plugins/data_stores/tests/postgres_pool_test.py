"""What a Postgres deployment's pool says about itself (#818): its connections as they stand, and the requests it ran out for."""

from __future__ import annotations

import gc
import weakref
from http import HTTPStatus
from typing import TYPE_CHECKING

import sqlalchemy as sa
from flask import Flask
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.pool import QueuePool

from dlboard.conftest import scraped_total
from dlboard.plugins.data_stores.postgres import (
    _pool_exhausted,
    _pool_levels,
    _PoolAttributes,
    _PoolState,
)
from dlboard.serve.telemetry._prometheus import serve_metrics

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _levels(engine: sa.Engine) -> dict[_PoolState, float]:
    return {
        labels.state: value
        for value, labels in _pool_levels(weakref.ref(engine))
        if isinstance(labels, _PoolAttributes)
    }


def test_the_pool_says_how_many_connections_are_in_use_and_idle_as_they_stand(tmp_path: Path) -> None:
    """Read from the pool at each look, so a connection given back is idle at the next."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", poolclass=QueuePool, pool_size=2)

    with engine.connect():
        assert _levels(engine) == {_PoolState.IN_USE: 1, _PoolState.IDLE: 0}
    assert _levels(engine) == {_PoolState.IN_USE: 0, _PoolState.IDLE: 1}
    engine.dispose()


def test_a_store_gone_says_nothing(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}", poolclass=QueuePool)
    gone = weakref.ref(engine)
    del engine
    gc.collect()  # an engine holds itself in reference cycles: only the cycle collector lets it go

    assert _pool_levels(gone) == []


def test_a_request_that_waited_out_the_pool_is_a_503_to_try_again_and_counted(
    telemetry: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 500 told a client nothing it could do; the client's shipping retries a 503, after `Retry-After`."""
    telemetry.setenv("DLBOARD_TELEMETRY_METRICS", "prometheus")
    telemetry.setenv("DLBOARD_TELEMETRY_PROMETHEUS_DIR", str(tmp_path))
    app = Flask(__name__)
    app.register_error_handler(PoolTimeoutError, _pool_exhausted)

    @app.route("/busy")
    def busy() -> str:
        msg = "QueuePool limit of size 5 overflow 10 reached, connection timed out"
        raise PoolTimeoutError(msg)

    answered = app.test_client().get("/busy")

    assert answered.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert answered.headers["Retry-After"] == "5"
    assert scraped_total(serve_metrics().get_data(as_text=True), "dlboard_db_pool_timeouts_total") == 1
