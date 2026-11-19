"""TensorBoard event files, as the `tensorboard` package wrote them (`tensorboard_logdir/`), read without it (#702)."""

from __future__ import annotations

from pathlib import Path

from dlboard.client import _tfevents

_LOGDIR = Path(__file__).with_name("tensorboard_logdir")
(_BASELINE,) = (_LOGDIR / "baseline").iterdir()


def _summaries(path: Path) -> list[tuple[int, _tfevents.Summary]]:
    return [
        (event.step, summary)
        for _, record in _tfevents.records(path, 0)
        for event in [_tfevents.event(record)]
        for summary in event.summaries
    ]


def test_every_kind_of_summary_an_import_brings_across_is_read() -> None:
    summaries = _summaries(_BASELINE)

    scalars = [(step, s.tag, round(s.value, 4)) for step, s in summaries if isinstance(s, _tfevents.Scalar)]
    histograms = [(step, s) for step, s in summaries if isinstance(s, _tfevents.Histo)]
    pictures = [s for _, s in summaries if isinstance(s, _tfevents.Picture)]
    (hparams,) = [s for _, s in summaries if isinstance(s, _tfevents.Hparams)]

    # `loss` is a plain scalar, as PyTorch writes it, and `acc` a rank-0 tensor, as TF2 does.
    assert [(step, value) for step, tag, value in scalars if tag == "loss"] == [
        (0, 1.0),
        (1, 0.5),
        (2, 0.3333),
        (3, 0.25),
        (4, 0.2),
    ]
    assert [(step, value) for step, tag, value in scalars if tag == "acc"] == [(s, s / 10) for s in range(5)]
    assert [step for step, _ in histograms] == [0, 1]
    assert sum(histograms[0][1].histogram.counts) == 100
    assert pictures[0].png.startswith(b"\x89PNG")
    assert hparams.values == {"lr": 0.1, "optimizer": "adam", "deep": True}
    assert _tfevents.Unread("text") in [s for _, s in summaries]


def test_a_file_cut_short_is_read_to_its_last_whole_record(tmp_path: Path) -> None:
    whole = _BASELINE.read_bytes()
    ends = [end for end, _ in _tfevents.records(_BASELINE, 0)]
    cut = tmp_path / _BASELINE.name
    cut.write_bytes(whole[: ends[-2] + 5])  # the last record begun, and not finished

    assert [end for end, _ in _tfevents.records(cut, 0)] == ends[:-1]
    assert [end for end, _ in _tfevents.records(_BASELINE, ends[-2])] == ends[-1:]
