from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from dltrack.models import ChartInstance, NewPage, PanelInstance
from dltrack.serve._pages._experiment._views import ViewFile


def test_an_exported_view_imports_back_unchanged() -> None:
    page = NewPage[Any, Any](
        experiment_id=1,
        panels=[PanelInstance[Any, Any](name="loss", charts=[ChartInstance[Any, Any](chart_type="line")])],
        page_settings={"excluded_runs": [3]},
    )

    exported = ViewFile.of(page, name="mine").model_dump_json()
    imported = ViewFile.model_validate_json(exported)

    assert imported.name == "mine"
    assert imported.panels == page.panels
    assert imported.page_settings == page.page_settings


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{}",
        '{"name": "x", "panels": [], "dltrack_view": 2}',
        '{"name": "x", "panels": [{"charts": [{"parameters": {}}]}]}',
    ],
    ids=["empty", "not-a-view", "unknown-format-version", "chart-without-a-type"],
)
def test_anything_but_a_valid_view_is_rejected_on_import(raw: str) -> None:
    with pytest.raises(ValidationError):
        ViewFile.model_validate_json(raw)
