"""`DataRequest`: everything a chart reads, which is what its panel fetches for it."""

from __future__ import annotations

import dataclasses
import typing

from dlboard._compat import StrEnum
from dlboard.models._key_patterns import KeyPatterns
from dlboard.models._metric import MetricReductions

if typing.TYPE_CHECKING:
    from dlboard.models._panel_frame import SeriesBandRequest

EVERY_KEY: typing.Final = frozenset({"*"})
"""As a request's patterns: every key of that kind, however many were logged (see `KeyPatterns.everything`)."""

EVERY_STEP: typing.Final = MetricReductions()
"""Every metric read whole, a row per step: what a chart that draws its series reads."""


class WindowAt(StrEnum):
    """Which of the steps a key logged a window reads the artifacts of."""

    FIRST = "first"
    """The first `steps` steps any run logged: where a slider starts. The chart reads the next as it moves past them."""
    LATEST = "latest"
    """Each run's latest step."""
    THINNED = "thinned"
    """About `steps` of each run's steps: every 2nd, 4th, ... one from its first, and always its latest (`thinned`)."""


def thinned(steps: list[int], most: int) -> list[int]:
    """
    About `most` of `steps` (sorted): every `stride`-th from the first, and always the last.

    `stride` is the least power of two that fits, so the steps kept stay put as a run logs more, until it doubles,
    rather than shifting at every refresh (as `fetch_histograms` thins).
    """
    stride = 1
    while -(-len(steps) // stride) > most:
        stride *= 2
    kept = steps[::stride]
    return kept if kept[-1] == steps[-1] else [*kept, steps[-1]]


@dataclasses.dataclass(frozen=True)
class ArtifactWindow:
    """
    The artifacts under `key`, `with_keys` and `with_prefixes`, read at only some of the steps `key` logged (`at`).

    For a chart whose read would otherwise grow with the steps logged (an image chart's slider, a Plotly template's
    data): its panel lists every step `key` logged, by run, without their artifacts, and reads the artifacts of the
    window's steps alone. A step is two numbers, not an artifact.
    """

    key: str
    steps: int
    at: WindowAt = WindowAt.FIRST
    with_keys: frozenset[str] = frozenset()
    """Other keys read at the window's steps (an image's layers)."""
    with_prefixes: frozenset[str] = frozenset()
    """Keys starting with these, read at the window's steps (the overlays logged over an image)."""

    def selected(self, logged: dict[int, list[int]]) -> dict[int, set[int]]:
        """The steps of each run the window reads, of the ones each `logged` (in order, by run)."""
        match self.at:
            case WindowAt.FIRST:
                first = set(sorted({step for steps in logged.values() for step in steps})[: self.steps])
                return {run: first.intersection(steps) for run, steps in logged.items()}
            case WindowAt.LATEST:
                return {run: {steps[-1]} for run, steps in logged.items() if steps}
            case WindowAt.THINNED:
                return {run: set(thinned(steps, self.steps)) for run, steps in logged.items() if steps}


@dataclasses.dataclass(frozen=True)
class DataRequest:
    """
    Everything a chart reads, from its settings: what its panel fetches for it, and all the chart is handed.

    A chart asks for nothing it does not draw, so its panel reads no more than the charts on screen need. A kind of data
    it leaves out is not read for it at all. Names and patterns are apart because a name is literal: a metric called
    `top#1` is that metric, not a pattern over digits.
    """

    metrics: frozenset[str] = frozenset()
    """The metrics drawn, by name: `loss`, and `step` or `timestamp_utc` for an x-axis."""

    metric_patterns: frozenset[str] = frozenset()
    """
    The metrics drawn, by pattern (`layer_*/grad_norm`, see `KeyPatterns`), matched against the names logged when the
    panel is drawn, so metrics logged later are included; `EVERY_KEY` for every metric.

    A pattern over thousands of metrics must not read their whole history (a column for each metric and a row for each
    step): read their last value (`reductions`) or their spread (`series`).
    """

    reductions: MetricReductions = EVERY_STEP
    """
    How much of each metric the chart draws from: every step by default, or a row per run (`LATEST`, `FIRST`, `MIN`,
    `MAX`, `MEAN`) for a chart that shows where each run ended up or a summary of its series. That row is what keeps such
    a chart's cost from growing with training.
    """

    hparams: frozenset[str] = frozenset()
    """The hyperparameters drawn, by name. One small row per run, however many."""

    hparam_patterns: frozenset[str] = frozenset()
    """The hyperparameters drawn, by pattern; `EVERY_KEY` for every one."""

    artifacts: frozenset[str] = frozenset()
    """The artifact keys drawn, by name."""

    artifact_prefixes: frozenset[str] = frozenset()
    """
    Every artifact whose key starts with one of these, including ones logged later: for a chart that lists a family
    (`checkpoints/`) rather than named keys. A prefix is literal: a `*` or `#` in it is part of the key.
    """

    histograms: frozenset[str] = frozenset()
    """
    The logged histogram keys drawn. There is no "every histogram": a histogram is the most expensive thing a panel
    reads, so a chart names the ones it draws.
    """

    series: frozenset[SeriesBandRequest] = frozenset()
    """The spreads of many metrics' values the chart draws (`SeriesBandRequest`), read in place of the metrics."""

    artifact_windows: frozenset[ArtifactWindow] = frozenset()
    """
    Read the artifacts asked for at only some steps, through these windows (`ArtifactWindow`), and every step each
    window's key logged; none reads them at every step. A chart with windows reads its artifacts through them alone.
    """

    @property
    def every_metric(self) -> bool:
        """Whether the chart draws every metric logged: the read that grows with the experiment."""
        return KeyPatterns(tuple(self.metric_patterns)).everything

    def draws_metric(self, key: str) -> bool:
        """Whether the metric called `key` is one the chart draws, by name or by pattern."""
        return key in self.metrics or KeyPatterns(tuple(self.metric_patterns)).matches(key)
