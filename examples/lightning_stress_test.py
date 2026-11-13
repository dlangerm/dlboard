"""
`lightning_advanced.py`'s run, logging ten times as long and as often as it can: a load on the server, not a tour.

Its charts are not meant to be read. A slider over 250 epochs of previews is too fine to scrub, and
`DeviceStatsMonitor` alone adds ~500 series. What it is for is seeing how the server and the UI hold up under a
run that logs a lot: how fast the logger ships, how a page of hundreds of charts draws, how a long history reads.
Keep these numbers as they are, so one run of it compares with the last. What it found dlboard can't do yet:

1. Nothing to skip or collapse a noisy group: `DeviceStatsMonitor` alone adds ~500 series, so auto-generate
   offers 886 charts, and its panels sort first, ahead of `train` and `val`.
2. A panel header carries about 30 Mantine components (8 tooltips), so a view with hundreds of panels is slow
   to draw: 239 panels, which grouping this run by suffix makes, take about 14 s (#192).
"""

from lightning_advanced import Volume, main

STRESS = Volume(
    experiment="unet-stress",
    max_epochs=250,
    log_every_n_steps=5,
    grad_histogram_every_n_steps=10,
    preview_every_n_epochs=1,
    device_stats=True,
)


if __name__ == "__main__":
    main(STRESS)
