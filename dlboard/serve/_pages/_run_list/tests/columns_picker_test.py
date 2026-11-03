"""The Columns picker's options."""

from __future__ import annotations

from dlboard.serve._pages._experiment._column_catalog import KEY_PICKER_LIMIT
from dlboard.serve._pages._run_list._columns_picker import column_options


def test_the_columns_picker_offers_a_page_of_what_its_search_finds_and_keeps_its_picks() -> None:
    """Every metric name went to the browser with the picker (#416): a page of them, the rest a search away."""
    hparams = ["batch_size", "lr"]
    metrics = [f"metric_{i:05d}" for i in range(KEY_PICKER_LIMIT * 3)]

    listed = column_options(hparams, metrics)
    searched = column_options(hparams, metrics, search="00250", keep=["metric_00001", "lr"])

    assert [len(group["items"]) for group in listed] == [2, KEY_PICKER_LIMIT - 2]
    assert [group["items"] for group in searched] == [["lr"], ["metric_00001", "metric_00250"]]
