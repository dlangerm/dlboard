"""
Renders the screenshots embedded in the docs from the same browser harness `browser_test.py` uses.

The PNGs in `docs/images/` are committed, and `test_doc_screenshot` re-renders each one and fails if
it no longer matches -- so a UI change that isn't reflected in the docs (or a bug that changes how
the app looks) fails CI instead of going stale. Font rendering differs between machines, so the
committed images are the ones CI renders: on a mismatch CI uploads `screenshot-diffs/` (the fresh
render plus a diff image) as a workflow artifact, to be copied over `docs/images/` and committed.

Run with `--screenshots=check` (compare) or `--screenshots=update` (overwrite `docs/images/`); see
`dltrack/conftest.py` for how those runs are isolated from every other test.
"""

from __future__ import annotations

import io
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import numpy as np
import pendulum
import pytest
from PIL import Image as PILImage
from playwright.sync_api import expect

from dltrack import models
from dltrack.conftest import ScreenshotMode
from dltrack.plugins.artifacts.image import Image
from dltrack.plugins.backend.basic_rest_backend import BasicDltrackAPI
from dltrack.serve import get_data_store
from dltrack.serve._pages._experiment._chart_autogen import ARTIFACT_PANEL_SUFFIX
from dltrack.serve._pages._experiment._experiment_page_state import PAGE_EXPERIMENT_ID

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dash import Dash
    from playwright.sync_api import Locator, Page

REPO_ROOT = Path(__file__).parents[2]
IMAGES_DIR = REPO_ROOT / "docs" / "images"
DIFFS_DIR = REPO_ROOT / "screenshot-diffs"

_VIEWPORT = {"width": 1440, "height": 900}
_CHANNEL_TOLERANCE = 8
"""Per-channel difference (out of 255) below which two pixels count as identical (anti-aliasing noise)."""
_MAX_DIFFERING_PIXEL_FRACTION = 0.001
_STABLE_ATTEMPTS = 20
_ARTIFACT_INGEST_TIMEOUT_S = 30

_T0 = pendulum.datetime(2026, 1, 1, 9, tz="UTC")
_N_STEPS = 60
_IMAGE_STEPS = (0, 20, 40, 59)
_PROJECT_NAME = "image-classification"
_EXPERIMENT_NAME = "lr-sweep"
_CONFUSION_KEY = "samples/confusion_matrix"


class DocScreenshot(StrEnum):
    """Every screenshot the docs embed; the value is the PNG's name in `docs/images/`."""

    HOME = "home"
    HOME_LIGHT = "home-light"
    PROJECT = "project"
    EXPERIMENT_CHARTS = "experiment-charts"
    IMAGE_SERIES = "image-series"

    @property
    def path(self) -> Path:
        """Where the committed PNG lives."""
        return IMAGES_DIR / f"{self.value}.png"


class _Sweep(NamedTuple):
    name: str
    lr: float
    batch_size: int
    convergence_rate: float


_SWEEP = (
    _Sweep("lr=1e-2", 1e-2, 32, 0.09),
    _Sweep("lr=3e-3", 3e-3, 64, 0.07),
    _Sweep("lr=1e-3", 1e-3, 64, 0.04),
    _Sweep("lr=3e-4", 3e-4, 128, 0.02),
)


class Demo(NamedTuple):
    """IDs of the seeded data the scenes navigate to."""

    url: str
    project_id: int
    experiment_id: int
    n_projects: int
    n_experiments: int


def _confusion_matrix(step: int, rng: np.random.Generator) -> np.ndarray:
    """A 10-class confusion heatmap that sharpens along the diagonal as `step` grows."""
    progress = step / _N_STEPS
    counts = rng.random((10, 10)) * (1 - progress) * 20 + np.eye(10) * (20 + 80 * progress)
    return np.kron(255 - counts / counts.max() * 255, np.ones((16, 16))).astype(np.uint8)


@pytest.fixture(scope="session")
def demo(live_server_url: str, dltrack_app: Dash, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Demo]:
    """Seed the app, through the real REST client, with a small deterministic body of demo data."""
    # The app shows who's logged in (the OS login name, unless told otherwise), which would make
    # every machine's screenshots differ -- pin it for the whole run.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DLTRACK_USER", "demo")
    api = BasicDltrackAPI(live_server_url)
    rng = np.random.default_rng(0)
    artifact_dir = tmp_path_factory.mktemp("demo-artifacts")

    def project(name: str, description: str) -> models.Project:
        return api.create_project(models.NewProject(name=name, description=description, created_at=_T0))

    def experiment(project_id: int, name: str, description: str) -> models.Experiment:
        return api.create_experiment(
            models.NewExperiment(project_id=project_id, name=name, description=description, created_at=_T0)
        )

    main = project(_PROJECT_NAME, "ResNet-18 on CIFAR-10: learning-rate and augmentation studies.")
    project("speech-recognition", "Streaming conformer models on LibriSpeech.")
    project("text-summarization", "Fine-tuning small seq2seq models on long-form documents.")
    sweep = experiment(main.id, _EXPERIMENT_NAME, "Learning-rate sweep with AdamW, 60 steps per run.")
    experiment(main.id, "augmentation-ablation", "Which of RandAugment, MixUp and CutMix earns its keep.")
    experiment(main.id, "baseline", "The reference run every other experiment is compared against.")

    for index, config in enumerate(_SWEEP):
        run = api.create_run(
            models.NewRun(experiment_id=sweep.id, name=config.name, created_at=_T0.add(hours=index))
        )
        api.log_hyperparams(
            models.NewHyperParams.from_raw(
                run.id,
                sweep.id,
                {"lr": config.lr, "batch_size": config.batch_size, "optimizer": "adamw"},
            )
        )
        steps = np.arange(_N_STEPS)
        train_loss = 0.15 + 2.0 * np.exp(-steps * config.convergence_rate) + rng.normal(0, 0.02, _N_STEPS)
        val_loss = 0.3 + 1.9 * np.exp(-steps * config.convergence_rate * 0.9) + rng.normal(0, 0.03, _N_STEPS)
        val_acc = 0.93 - 0.6 * np.exp(-steps * config.convergence_rate) + rng.normal(0, 0.01, _N_STEPS)
        api.log_metric_batch(
            [
                models.LoggedMetrics(
                    experiment_id=sweep.id,
                    run_id=run.id,
                    step=step,
                    metrics={
                        "loss/train": float(train_loss[step]),
                        "loss/val": float(val_loss[step]),
                        "acc/val": float(val_acc[step]),
                    },
                    timestamp_utc=_T0.add(hours=index, minutes=step),
                )
                for step in range(_N_STEPS)
            ]
        )
        converted = [
            Image(key=_CONFUSION_KEY, image=_confusion_matrix(step, rng), step=step).to_artifact(
                artifact_dir, run.id, sweep.id
            )
            for step in _IMAGE_STEPS
        ]
        api.log_artifact_batch([artifact for artifact, _ in converted], [path for _, path in converted])
    # The server records uploaded artifacts on a background batch (see `FSArtifactStore`), so
    # they land shortly after the uploads above return.
    store = get_data_store(dltrack_app)
    expected = len(_SWEEP) * len(_IMAGE_STEPS)
    deadline = time.monotonic() + _ARTIFACT_INGEST_TIMEOUT_S
    while len(list(store.fetch_artifacts(experiment_id=sweep.id))) < expected:
        assert time.monotonic() < deadline, "the demo artifacts were never recorded"
        time.sleep(0.2)
    try:
        yield Demo(live_server_url, main.id, sweep.id, n_projects=3, n_experiments=3)
    finally:
        monkeypatch.undo()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """A fixed, animation-free, dark-mode window so every machine frames the same page."""
    return {
        **browser_context_args,
        "viewport": _VIEWPORT,
        "device_scale_factor": 1,
        "color_scheme": "dark",
        "reduced_motion": "reduce",
    }


def _open_charted_experiment(page: Page, demo: Demo) -> None:
    """
    Open the demo experiment with its panels generated, whichever scene got here first.

    Panels persist in the shared database, and the button only exists while there are none.
    """
    page.goto(f"{demo.url}/experiment/{demo.experiment_id}")
    expect(page.locator(f"#{PAGE_EXPERIMENT_ID}")).to_be_visible()
    button = page.get_by_role("button", name="Auto-generate charts")
    if button.count():
        button.click()


def _stage(shot: DocScreenshot, page: Page, demo: Demo) -> Page | Locator:
    """Drive the real UI to `shot`'s state, and return what to photograph."""
    match shot:
        case DocScreenshot.HOME:
            page.goto(demo.url)
            expect(page.locator(".project-card")).to_have_count(demo.n_projects)
            return page
        case DocScreenshot.HOME_LIGHT:
            page.goto(demo.url)
            expect(page.locator(".project-card")).to_have_count(demo.n_projects)
            page.get_by_role("button", name="Toggle light/dark mode").click()
            expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", "light")
            page.mouse.move(0, _VIEWPORT["height"] - 1)  # off the toggle, so it isn't shown hovered
            return page
        case DocScreenshot.PROJECT:
            page.goto(f"{demo.url}/project/{demo.project_id}")
            expect(page.locator(".experiment-card")).to_have_count(demo.n_experiments)
            return page
        case DocScreenshot.EXPERIMENT_CHARTS:
            _open_charted_experiment(page, demo)
            expect(page.locator(".dl-panel-body svg").first).to_be_visible()
            return page
        case DocScreenshot.IMAGE_SERIES:
            _open_charted_experiment(page, demo)
            page.get_by_text(f"samples{ARTIFACT_PANEL_SUFFIX}").click()
            panel = page.locator(".dl-panel-body", has=page.locator("img"))
            expect(panel).to_be_visible()
            page.wait_for_function("[...document.images].every(i => i.complete && i.naturalWidth > 0)")
            return panel


def _stable_screenshot(target: Page | Locator) -> bytes:
    """
    Photograph `target` until two consecutive captures are byte-identical.

    Chart animations and live-update re-renders are JavaScript, so `animations="disabled"` can't
    stop them; waiting for a fixed point does.
    """
    previous: bytes | None = None
    for _ in range(_STABLE_ATTEMPTS):
        current = target.screenshot(animations="disabled", caret="hide")
        if current == previous:
            return current
        previous = current
        time.sleep(0.3)
    msg = "the page never stopped changing"
    raise AssertionError(msg)


def _matches_committed(shot: DocScreenshot, png: bytes) -> bool:
    """Compare against the committed image, writing the fresh render and a diff for CI to upload if not."""
    fresh = np.asarray(PILImage.open(io.BytesIO(png)).convert("RGB"), dtype=int)
    differing = None
    if shot.path.exists():
        committed = np.asarray(PILImage.open(shot.path).convert("RGB"), dtype=int)
        if committed.shape == fresh.shape:
            differing = np.abs(fresh - committed).max(axis=2) > _CHANNEL_TOLERANCE
            if differing.mean() <= _MAX_DIFFERING_PIXEL_FRACTION:
                return True
    for subdir in ("actual", "diff"):
        (DIFFS_DIR / subdir).mkdir(parents=True, exist_ok=True)
    (DIFFS_DIR / "actual" / shot.path.name).write_bytes(png)
    if differing is not None:
        PILImage.fromarray((differing * 255).astype(np.uint8)).save(DIFFS_DIR / "diff" / shot.path.name)
    return False


@pytest.mark.screenshots
@pytest.mark.parametrize("shot", list(DocScreenshot))
def test_doc_screenshot(
    shot: DocScreenshot,
    page: Page,
    demo: Demo,
    console_errors: list[str],
    screenshot_mode: ScreenshotMode,
) -> None:
    """The committed docs image for `shot` is what the app renders today (or is rewritten to be)."""
    target = _stage(shot, page, demo)
    page.evaluate("document.fonts.ready")  # a late-swapping web font would otherwise reflow mid-capture
    png = _stable_screenshot(target)
    assert console_errors == []
    match screenshot_mode:
        case ScreenshotMode.UPDATE:
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            shot.path.write_bytes(png)
        case ScreenshotMode.CHECK:
            assert _matches_committed(shot, png), (
                f"{shot.path.relative_to(REPO_ROOT)} is stale or missing. Copy the fresh render from "
                f"screenshot-diffs/actual/ (CI uploads it as the `doc-screenshots` artifact) over it and commit."
            )


def test_doc_images_are_referenced_and_accounted_for() -> None:
    """Every screenshot is embedded in some doc, and no stray image sits in `docs/images/`."""
    docs = "\n".join(
        path.read_text() for path in [REPO_ROOT / "README.md", *(REPO_ROOT / "docs").rglob("*.md")]
    )
    assert [shot for shot in DocScreenshot if f"images/{shot.value}.png" not in docs] == []
    known = {shot.path.name for shot in DocScreenshot}
    assert {path.name for path in IMAGES_DIR.glob("*.png")} <= known
