"""
Unit tests for `dlboard.client.run`, with no real processes or network (see `conftest.py`'s fakes).

`dlboard/tests/logger_shipping_test.py` and `run_shipping_test.py` cover the real thing end to end against a live server.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import queue
import sys
import threading
import time
import warnings
from argparse import Namespace
from enum import Enum
from typing import TYPE_CHECKING, Any

import matplotlib.figure
import numpy as np
import PIL.Image
import pydantic
import pydantic.dataclasses
import pytest
import requests
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from dlboard import models
from dlboard._batching import Failure
from dlboard._mp_context import SPAWN_CONTEXT
from dlboard.client import _context, run
from dlboard.client._console import ConsoleChunk, ConsoleSettings
from dlboard.client.artifacts.figure import Figure
from dlboard.client.artifacts.image import Image
from dlboard.client.artifacts.overlay import Overlay
from dlboard.client.histograms import ExplicitBinning, ExponentialBinning

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dlboard.client.tests.conftest import FakeAPI, FakeShipper


@pytest.fixture
def started(fake_api: FakeAPI, shippers: list[FakeShipper]) -> Iterator[run.Run]:
    """A run `init` started, finished once the test is done with it (so its exit hook has nothing to do)."""
    del fake_api, shippers
    started = run.init("proj")
    yield started
    started.finish()


def test_init_starts_a_named_run_in_the_project_and_experiment_it_names(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    started = run.init(
        "proj",
        "exp",
        name="baseline",
        project_description="desc",
        hparams={"lr": 0.1},
        pin=models.PinColor.RED,
    )

    assert [(call, args) for call, args, _ in fake_api.calls] == [
        ("get_or_create_project", ("proj",)),
        ("get_or_create_experiment", (1,)),
        ("create_run", (2,)),
        ("mark_run", (99,)),
        ("record_run_context", (99,)),
        ("log_hyperparams", ({"lr": 0.1},)),
    ]
    assert fake_api.calls[0][2] == {"description": "desc"}
    assert fake_api.calls[1][2]["source"] is models.ExperimentSource.PYTHON
    assert fake_api.calls[2][2] == {"name": "baseline"}
    assert (started.run_id, started.experiment_id, len(shippers)) == (99, 2, 2)
    started.finish()


def test_a_new_run_records_what_produced_it_and_queues_its_uncommitted_changes(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    fake_api.diff = _context.Diff(data=b"--- a/train.py\n+++ b/train.py\n")

    with run.init("proj"):
        pass

    assert fake_api.called("record_run_context") == [(99,)]
    _metrics, artifacts = shippers
    ((diff,),) = artifacts.items
    assert (diff.key, diff.tags) == (
        models.RUN_CONTEXT_DIFF_KEY,
        {models.FILE_KIND_TAG: models.FileKind.DIFF.value},
    )


def test_a_run_attached_to_keeps_the_context_it_was_started_with(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del shippers
    with run.init("proj", run_id=7):
        pass

    assert fake_api.called("record_run_context") == []


def test_a_server_that_cannot_record_the_context_never_fails_the_script(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    """An older server has no route for it: the run goes on, and says what it could not record."""
    del shippers
    fake_api.context_error = requests.HTTPError("404 Not Found")

    with (
        pytest.warns(UserWarning, match="could not record what produced this run"),
        run.init("proj") as started,
    ):
        started.log_metrics({"loss": 1.0}, step=0)

    assert started.status is models.RunStatus.FINISHED


def test_a_run_given_no_name_is_named_by_the_server_model(fake_api: FakeAPI, started: run.Run) -> None:
    """`NewRun` makes a name up only when the field is omitted: passing `None` through would leave it unnamed."""
    del started
    ((_, _, kwargs),) = [call for call in fake_api.calls if call[0] == "create_run"]
    assert kwargs["name"]


@pytest.mark.parametrize(
    ("raises", "status"), [(False, models.RunStatus.FINISHED), (True, models.RunStatus.FAILED)]
)
def test_a_run_used_as_a_context_manager_finishes_as_its_block_ended(
    fake_api: FakeAPI, shippers: list[FakeShipper], *, raises: bool, status: models.RunStatus
) -> None:
    del shippers
    block_ends = pytest.raises(RuntimeError) if raises else contextlib.nullcontext()
    with block_ends, run.init("proj") as started:
        started.log_metrics({"loss": 1.0}, step=0)
        if raises:
            raise RuntimeError

    assert fake_api.called("finish_run") == [(99, status)]
    assert started.status is status


@pytest.mark.parametrize("variable", ["RANK", "LOCAL_RANK", "SLURM_PROCID", "JSM_NAMESPACE_RANK"])
def test_a_non_zero_rank_makes_no_server_calls_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, shippers: list[FakeShipper], variable: str
) -> None:
    monkeypatch.setenv(variable, "1")
    monkeypatch.setenv("DLBOARD_RUN_ID", "7")

    with run.init("proj", hparams={"lr": 0.1}) as started:
        started.log_metrics({"loss": 1.0}, step=0)
        started.log_artifact([_picture("a")])

    assert (fake_api.calls, shippers) == ([], [])
    assert (started.run_id, started.experiment_id) == (7, None)


def test_rank_is_read_as_lightning_reads_it() -> None:
    assert run.LaunchEnv(rank=None, local_rank=None).global_rank == 0
    assert run.LaunchEnv(rank=0, local_rank=3).global_rank == 0  # the global rank wins over the node's own


def test_a_run_never_finished_is_flushed_and_reported_unknown_at_exit(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    """A script that ends without finishing its run would otherwise read as "running" forever."""
    started = run.init("proj")
    started.log_metrics({"loss": 1.0}, step=0)

    with pytest.warns(UserWarning, match="never finished"):
        started._finish_at_exit()

    assert all(shipper.flushed for shipper in shippers)
    assert fake_api.called("finish_run") == [(99, models.RunStatus.UNKNOWN)]


def test_the_exit_hook_leaves_a_finished_run_alone(fake_api: FakeAPI, started: run.Run) -> None:
    started.finish()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        started._finish_at_exit()

    assert fake_api.called("finish_run") == [(99, models.RunStatus.FINISHED)]


def test_a_server_that_cannot_record_the_end_never_fails_the_script(
    fake_api: FakeAPI, started: run.Run
) -> None:
    fake_api.finish_error = requests.ConnectionError("down")

    with pytest.warns(UserWarning, match="could not record that the run failed"):
        started.finish(models.RunStatus.FAILED)

    assert started.status is None
    fake_api.finish_error = None


def test_a_warning_points_at_the_line_that_logged(started: run.Run) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        started.log_artifact([_overlay("a")])

    (warning,) = caught
    assert warning.filename == __file__


def test_log_figure_queues_a_png_rendered_at_the_call_site(
    started: run.Run, shippers: list[FakeShipper]
) -> None:
    _metrics, artifacts = shippers
    fig = matplotlib.figure.Figure()
    fig.subplots().plot([1, 2, 3])

    started.log_figure(fig, "loss_curve", step=4, tags={"split": "val"}, save_kwargs={"dpi": 50})
    fig.clear()  # callers close or reuse the figure right after logging -- the queued PNG mustn't change

    (queued,) = artifacts.items
    (figure,) = queued
    assert isinstance(figure, Figure)
    assert (figure.key, figure.step, figure.tags) == ("loss_curve", 4, {"split": "val"})
    assert figure.format == "png"
    assert PIL.Image.open(io.BytesIO(figure.data)).size == (320, 240)


_PICTURE = np.zeros((4, 4, 3), dtype=np.uint8)


def _picture(key: str, step: int = 0) -> Image:
    return Image(key=key, image=_PICTURE, step=step)


def _overlay(image_key: str, step: int = 0, name: str | None = None) -> Overlay:
    return Overlay.over(image_key, name=name, shapes=[], step=step)


@pytest.mark.parametrize(
    ("batches", "warning"),
    [
        pytest.param(
            [[_picture("a"), _overlay("a", name="truth"), _overlay("a", name="pred")]], None, id="named"
        ),
        pytest.param(
            [[_overlay("a")]], "nothing is logged under that key in this call", id="overlay-of-no-image"
        ),
        pytest.param(
            [[_picture("a"), _overlay("a"), _overlay("a")]],
            "is logged twice at step 0",
            id="unnamed-twice",
        ),
        pytest.param([[_picture("a", 1)], [_picture("a", 2)]], None, id="same-key-at-other-steps"),
    ],
)
def test_a_call_that_logs_an_overlay_without_its_image_or_a_key_twice_at_a_step_warns(
    started: run.Run, batches: list[list[Image | Overlay]], warning: str | None
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for batch in batches:
            started.log_artifact(batch)

    messages = [str(w.message) for w in caught]
    assert len(messages) == (warning is not None)
    assert all((warning or "") in message for message in messages)


def test_log_histograms_queues_buckets_not_values(started: run.Run, shippers: list[FakeShipper]) -> None:
    metrics, _artifacts = shippers
    prebinned = models.Histogram(edges=[0.0, 1.0], counts=[7])
    values = np.array([0.5, 1.5, 100.0])

    started.log_histograms(
        {"grad": values, "ready": prebinned}, step=4, binning=ExplicitBinning(edges=[0.0, 1.0, 2.0])
    )
    values[:] = 0  # callers reuse the buffer right after logging -- what was queued must not change

    (queued,) = metrics.items
    assert (queued.step, queued.run_id, queued.metrics) == (4, 99, {})
    assert queued.histograms["grad"] == models.Histogram(edges=[0.0, 1.0, 2.0, 100.0], counts=[1, 1, 1])
    assert queued.histograms["ready"] is prebinned


def test_log_histograms_raises_at_the_call_when_the_binning_cannot_fit(
    started: run.Run, shippers: list[FakeShipper]
) -> None:
    metrics, _artifacts = shippers

    with pytest.raises(pydantic.ValidationError, match="at most"):
        started.log_histograms(
            {"grad": 10.0 ** np.linspace(-30, 30, 5000)},
            step=0,
            binning=ExponentialBinning(buckets_per_decade=500),
        )

    assert metrics.items == []


@given(keys=st.integers(1, 2_500))
@example(keys=1_000)
@example(keys=1_001)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])  # one run logs every example's call
def test_a_wide_call_is_queued_as_items_of_bounded_width_that_together_hold_every_key_in_order(
    started: run.Run, shippers: list[FakeShipper], keys: int
) -> None:
    """A shipped batch is a fixed number of items, so each must be of bounded width however many keys a step logs."""
    metrics = {f"metric_{i:05}": float(i) for i in range(keys)}
    before = len(shippers[0].items)

    started.log_metrics(metrics, step=7)

    queued: list[models.LoggedMetrics] = shippers[0].items[before:]
    assert [len(item.metrics) for item in queued] == [min(1_000, keys - at) for at in range(0, keys, 1_000)]
    assert {(item.step, item.timestamp_utc) for item in queued} == {(7, queued[0].timestamp_utc)}
    assert {key: value for item in queued for key, value in item.metrics.items()} == metrics
    assert [key for item in queued for key in item.metrics] == list(metrics)


def test_log_hyperparams_sends_a_nested_config_flattened(fake_api: FakeAPI, started: run.Run) -> None:
    started.log_hyperparams({"optim": _Schedule(), "seed": 1})

    assert fake_api.called("log_hyperparams") == [
        ({"optim/kind": "adamw", "optim/betas": "[0.9, 0.999]", "seed": 1},)
    ]


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("down"), _http_error(500)], ids=["down", "refused"]
)
def test_hyperparameters_the_server_cannot_take_never_fail_the_script(
    fake_api: FakeAPI, started: run.Run, error: requests.RequestException
) -> None:
    """Lightning logs them as `fit` starts: a server blip then must not end the training run."""
    fake_api.hparams_error = error

    with pytest.warns(UserWarning, match="could not record this run's hyperparameters"):
        started.log_hyperparams({"lr": 0.1})


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (requests.ConnectionError(), Failure.TRANSIENT),
        (requests.Timeout(), Failure.TRANSIENT),
        (_http_error(503), Failure.TRANSIENT),
        (_http_error(408), Failure.TRANSIENT),
        (_http_error(429), Failure.TRANSIENT),
        # About what was sent: the batch is split, so only the item at fault is lost.
        (_http_error(400), Failure.PER_ITEM),
        (_http_error(409), Failure.PER_ITEM),
        (_http_error(413), Failure.PER_ITEM),
        (_http_error(422), Failure.PER_ITEM),
        (FileNotFoundError("a `File` whose path is gone"), Failure.PER_ITEM),
        (requests.exceptions.InvalidJSONError(), Failure.PER_ITEM),
        # About the request itself: nothing in it can land.
        (_http_error(403), Failure.WHOLE),
        (_http_error(404), Failure.WHOLE),
        (_http_error(423), Failure.WHOLE),
    ],
)
def test_classify_failure_retries_only_what_a_retry_can_fix(failure: Exception, kind: Failure) -> None:
    assert run.classify_failure(failure) is kind


class _FakeProcess:
    """`alive` is a plain mutable attribute, not init-only -- a real process can die mid-test too."""

    exitcode: int | None = None

    def __init__(self, *, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.alive = False

    def join(self, timeout: float) -> None:
        del timeout


class _FakeQueue(queue.Queue[str | None]):
    """A plain `queue.Queue`, plus the two methods of a `multiprocessing.Queue` a worker is stopped with."""

    def cancel_join_thread(self) -> None:
        pass

    def close(self) -> None:
        pass


def _worker(*, alive: bool = True, maxsize: int = 0) -> run._Worker[str]:
    """
    A real `_Worker` over test doubles: a plain queue (not a real `multiprocessing.Queue`, which needs a spawned
    process on the other end) and `threading.Event` (not `multiprocessing.synchronize.Event`) -- structurally
    identical for every method `_Shipper` calls.
    """
    return run._Worker(
        _FakeQueue(maxsize=maxsize),  # pyrefly: ignore [bad-argument-type]
        _FakeProcess(alive=alive),  # pyrefly: ignore [bad-argument-type]
        threading.Event(),  # pyrefly: ignore [bad-argument-type]
        threading.Event(),  # pyrefly: ignore [bad-argument-type]
        SPAWN_CONTEXT.Value("q", 0),
    )


def _shipper(*workers: run._Worker[str]) -> run._Shipper[str]:
    """A real `_Shipper` that starts each of `workers` in turn: the first at once, the rest as it restarts."""
    return run._Shipper("test", iter(workers).__next__)


def _killed(worker: run._Worker[str]) -> None:
    assert isinstance(worker.process, _FakeProcess)
    worker.process.alive = False


def test_shipper_put_drops_rather_than_blocks_once_the_queue_is_full() -> None:
    """Regression: training must never stall on dlboard falling behind -- the old blocking `put` could."""
    shipper = _shipper(_worker(maxsize=1))
    shipper.put("a")

    with pytest.warns(UserWarning, match="queue is full"):
        shipper.put("b")

    assert shipper.worker.queue.get_nowait() == "a"
    assert shipper.worker.queue.empty()  # "b" was dropped, never queued
    assert shipper._dropped_total == 1


def test_shipper_starts_a_process_that_died_again_rather_than_fail_training() -> None:
    """Regression: a dead shipping process used to make the next `log_*` call raise, ending the training run."""
    first, second = _worker(), _worker()
    shipper = _shipper(first, second)
    _killed(first)

    with pytest.warns(UserWarning, match="died"):
        shipper.put("a")  # too soon after it started: dropped, and started again on a later call
    shipper.started_at -= run._RESTART_INTERVAL_SEC
    with pytest.warns(UserWarning, match="started again"):
        shipper.put("b")

    assert shipper.worker is second
    assert second.queue.get_nowait() == "b"


def test_threads_that_find_the_process_dead_at_once_start_it_again_once() -> None:
    """The training loop, the system sampler and the console capture all put: one restart, and nothing left behind."""
    first = _worker()
    spares = [_worker() for _ in range(8)]
    workers = iter([first, *spares])

    def start() -> run._Worker[str]:
        time.sleep(0.02)  # long enough for every thread to find the process dead before the restart is done
        return next(workers)

    shipper = run._Shipper("test", start)
    _killed(first)
    shipper.started_at -= run._RESTART_INTERVAL_SEC
    putting = [threading.Thread(target=shipper.put, args=(str(i),)) for i in range(8)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for thread in putting:
            thread.start()
        for thread in putting:
            thread.join()

    assert shipper.worker is spares[0]
    assert {spares[0].queue.get_nowait() for _ in range(8)} == {str(i) for i in range(8)}
    assert all(spare.queue.empty() for spare in spares)


def test_shipper_flush_gives_up_at_its_deadline_and_says_what_the_server_refused() -> None:
    """Nothing drains these queues, so only the deadline ends the wait: a flush is bounded however long it takes."""
    shipper = _shipper(_worker())
    shipper.put("a")
    shipper.worker.refused.value = 3

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        shipped = shipper.flush(time.monotonic())

    assert not shipped
    assert shipper.unflushed
    messages = [str(w.message) for w in caught]
    assert any("refused 3 test" in m for m in messages)
    assert any("still hadn't all been shipped" in m for m in messages)


def test_shipper_flush_warns_and_returns_instead_of_hanging_on_a_dead_process() -> None:
    worker = _worker()
    shipper = _shipper(worker)
    shipper.put("a")
    _killed(worker)

    with pytest.warns(UserWarning, match="dead"):
        assert not shipper.flush(time.monotonic() + 60)

    assert worker.queue.get_nowait() == "a"  # no flush sentinel was queued for nothing to ever drain


def test_shipper_flush_warns_once_with_the_total_dropped_this_run() -> None:
    worker = _worker(maxsize=1)
    shipper = _shipper(worker)
    shipper.put("a")
    with pytest.warns(UserWarning, match="queue is full"):
        shipper.put("b")
    shipper.put("c")  # dropped too, but rate-limited: no second warning right after the first

    # Dead now, so `flush()` doesn't block waiting on `flushed` -- nothing would ever set it here.
    _killed(worker)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        shipper.flush(time.monotonic() + 60)

    assert any("dropped 2 test" in str(w.message) for w in caught)


def test_warn_if_startup_was_slow_warns_past_the_threshold() -> None:
    with pytest.warns(UserWarning, match="took 3.0s to start up"):
        run.warn_if_startup_was_slow(3.0)


def test_warn_if_startup_was_slow_is_silent_within_the_threshold() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        run.warn_if_startup_was_slow(0.1)


class _Optimizer(Enum):
    ADAMW = "adamw"


class _Schedule(pydantic.BaseModel):
    kind: _Optimizer = _Optimizer.ADAMW
    betas: tuple[float, float] = (0.9, 0.999)


@pydantic.dataclasses.dataclass
class _PydanticDataclass:
    warmup: int = 100


@dataclasses.dataclass
class _StdlibDataclass:
    flip: bool = True


class _Config(pydantic.BaseModel):
    lr: float = 1e-3
    schedule: _Schedule = _Schedule()
    warmup: _PydanticDataclass = _PydanticDataclass()
    renamed: int = pydantic.Field(default=3, serialization_alias="alias")

    @pydantic.field_serializer("lr")
    def _as_text(self, lr: float) -> str:
        return f"{lr:.0e}"


_SCALARS = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=3)
_CONFIG_KEYS = st.text("ab/", max_size=3)  # few, with slashes, so a joined name is often given twice


def _nested(inner: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    return st.dictionaries(_CONFIG_KEYS, _SCALARS | inner, max_size=3)


_CONFIGS = st.recursive(st.dictionaries(_CONFIG_KEYS, _SCALARS, max_size=3), _nested, max_leaves=10)


def _paths(config: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Each value of `config` by the path of keys to it, in order; an empty nested config is `{}`."""
    return [
        (path, value)
        for key, nested in config.items()
        for path, value in (
            _paths(nested, f"{prefix}/{key}" if prefix else key)
            if isinstance(nested, dict) and nested
            else [(f"{prefix}/{key}" if prefix else key, "{}" if isinstance(nested, dict) else nested)]
        )
    ]


@given(_CONFIGS)
@example({"a/b": 1, "a": {"b": 2}})  # #775: two values joined to one name
@example({"scheduler_kwargs": {}})  # #775: an empty config is still logged
def test_flatten_hparams_names_each_value_of_a_nested_config_by_its_path(config: dict[str, Any]) -> None:
    """Two values joined to one name keep the later, with a warning naming it."""
    paths = _paths(config)

    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")
        flat = run.flatten_hparams(config)

    assert flat == dict(paths)
    assert bool(warned) is (len(dict(paths)) < len(paths))


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param(
            {"fraction": np.float32(0.5), "n": np.int64(3)}, {"fraction": 0.5, "n": 3}, id="numpy-scalars"
        ),
        pytest.param(Namespace(a=1, b=Namespace(c="x")), {"a": 1, "b/c": "x"}, id="namespace"),
        pytest.param(
            {"opt": _Optimizer.ADAMW, "ids": [1, 2]}, {"opt": "adamw", "ids": "[1, 2]"}, id="enum-and-list"
        ),
        pytest.param({"aug": _StdlibDataclass()}, {"aug/flip": True}, id="stdlib-dataclass"),
        pytest.param({"data": _PydanticDataclass(warmup=5)}, {"data/warmup": 5}, id="pydantic-dataclass"),
        pytest.param(
            {"optim": _Config(), "seed": 1},
            {
                "optim/lr": "1e-03",
                "optim/schedule/kind": "adamw",
                "optim/schedule/betas": "[0.9, 0.999]",
                "optim/warmup/warmup": 100,
                "optim/alias": 3,
                "seed": 1,
            },
            id="nested-pydantic-honours-its-own-serializers",
        ),
        pytest.param(_Schedule(), {"kind": "adamw", "betas": "[0.9, 0.999]"}, id="a-model-itself"),
        pytest.param({"layer": _Config}, {"layer": str(_Config)}, id="unserializable-becomes-text"),
    ],
)
def test_flatten_hparams_joins_nested_configs_with_slashes(params: Any, expected: dict[str, Any]) -> None:  # noqa: ANN401
    assert run.flatten_hparams(params) == expected


def test_a_run_logs_its_system_metrics_at_its_training_step_until_it_finishes(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    """A step is only ever a training step: a sample is at the one the run last logged at, 0 before the first."""
    started = run.init("proj")
    (sampler,) = fake_api.samplers

    sampler.log({"system/cpu.utilization": 0.5})
    started.log_metrics({"loss": 1.0}, step=40)
    sampler.log({"system/cpu.utilization": 0.6})
    started.log_histograms({"grad": [0.5, 1.5]}, step=41)
    sampler.log({"system/cpu.utilization": 0.7})
    started.finish()

    samples = [item for item in shippers[0].items if "system/cpu.utilization" in item.metrics]
    assert [(sample.step, sample.metrics["system/cpu.utilization"]) for sample in samples] == [
        (0, 0.5),
        (40, 0.6),
        (41, 0.7),
    ]
    assert sampler.stopped


def test_a_non_zero_rank_samples_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del shippers
    monkeypatch.setenv("RANK", "1")

    run.init("proj").finish()

    assert fake_api.samplers == []


def test_a_run_captures_what_the_script_prints_until_it_finishes(
    fake_api: FakeAPI, shippers: list[FakeShipper]
) -> None:
    del fake_api
    streams = (sys.stdout, sys.stderr)
    started = run.init("proj", settings=run.RunSettings(console=ConsoleSettings(enabled=True)))
    print("epoch 1: loss 0.5")

    started.finish()
    print("not the run's")

    _metrics, artifacts = shippers
    ((chunk,),) = artifacts.items
    assert isinstance(chunk, ConsoleChunk)
    assert (chunk.key, chunk.data) == (models.CONSOLE_KEY, b"epoch 1: loss 0.5\n")
    assert (sys.stdout, sys.stderr) == streams


def test_a_runs_ids_can_be_bound_into_the_scripts_own_logs(started: run.Run) -> None:
    assert started.log_context == {"dlboard.run_id": 99, "dlboard.experiment_id": 2}
    assert run.Run(None).log_context == {}


def test_a_run_is_tagged_the_moment_it_exists(fake_api: FakeAPI, shippers: list[FakeShipper]) -> None:
    del shippers
    run.init("proj", tags=["baseline", "paper-fig-3"]).finish()

    ((_, _, marked),) = [call for call in fake_api.calls if call[0] == "mark_run"]
    assert marked == {"pin": None, "keep": False, "tags": ["baseline", "paper-fig-3"]}
