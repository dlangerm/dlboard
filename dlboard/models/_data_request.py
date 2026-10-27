"""`DataRequest`: everything a chart reads, which is what its panel fetches for it."""

from __future__ import annotations

import dataclasses
import typing

from dlboard.models._key_patterns import KeyPatterns
from dlboard.models._metric import MetricReductions

if typing.TYPE_CHECKING:
    from dlboard.models._panel_frame import SeriesBandRequest

EVERY_KEY: typing.Final = frozenset({"*"})
"""As a request's patterns: every key of that kind, however many were logged (see `KeyPatterns.everything`)."""

EVERY_STEP: typing.Final = MetricReductions()
"""Every metric read whole, a row per step: what a chart that draws its series reads."""


@dataclasses.dataclass(frozen=True)
class ArtifactWindow:
    """
    The artifacts a chart asks for, read at only `steps` of the steps `key` logged: the first ones.

    For a chart that shows one step at a time (an image chart's slider): its panel lists every step `key` logged, by
    run, without their artifacts, and reads the artifacts of the window alone, so what it reads does not grow with the
    steps logged. The chart reads the next window itself, as the viewer moves past this one.
    """

    key: str
    steps: int


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

    artifact_window: ArtifactWindow | None = None
    """
    Read the artifacts asked for at only a window of steps (`ArtifactWindow`), and every step its key logged; `None`
    reads them at every step.
    """

    @property
    def every_metric(self) -> bool:
        """Whether the chart draws every metric logged: the read that grows with the experiment."""
        return KeyPatterns(tuple(self.metric_patterns)).everything

    def draws_metric(self, key: str) -> bool:
        """Whether the metric called `key` is one the chart draws, by name or by pattern."""
        return key in self.metrics or KeyPatterns(tuple(self.metric_patterns)).matches(key)
