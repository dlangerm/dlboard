"""A tag a client logs reaches the chart's caption the way the artifacts dataframe hands it over: as JSON."""

from __future__ import annotations

import pytest

from dlboard.models import Artifact, Quantity, TagDisplay, TimeUnit, artifact_tags_column
from dlboard.plugins.charts._artifact_tags import format_tags_caption
from dlboard.serve._pages._experiment._panel_data import build_artifacts_dataframe


@pytest.mark.usefixtures("_plain_artifact_urls")
def test_a_quantity_tag_survives_the_artifacts_dataframe_into_its_caption() -> None:
    artifact = Artifact(
        id=1,
        key="img",
        fname="img.png",
        run_id=1,
        experiment_id=1,
        step=0,
        ref="ref://a",
        tags={"took": Quantity(value=5400, unit=TimeUnit.SECONDS), "split": "val"},
    )

    cell = build_artifacts_dataframe([artifact])[artifact_tags_column("img")].iloc[0]

    assert format_tags_caption(cell, TagDisplay(time_unit=TimeUnit.MINUTES)) == "took: 90.00 min, split: val"
