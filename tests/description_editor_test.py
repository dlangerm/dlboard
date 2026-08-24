# pyright: reportPrivateUsage=false
"""Tests for the shared editable name/description header used by the project and experiment pages."""

from __future__ import annotations

from typing import Any

from dltrack.plugins.pages._description_editor import DescriptionEditorIds, render_header
from tests.conftest import find_props as _find_props

_IDS = DescriptionEditorIds(
    header="h",
    edit_button="edit-btn",
    modal="modal",
    textarea="textarea",
    save="save",
    cancel="cancel",
)


def _all_text(component: Any) -> list[str]:  # noqa: ANN401
    """Collect every plain-string leaf under `component`, for substring assertions on rendered text."""
    if isinstance(component, str):
        return [component]
    if isinstance(component, list):
        return [
            text
            for item in component  # pyright: ignore[reportUnknownVariableType]
            for text in _all_text(item)
        ]
    if not hasattr(component, "to_plotly_json"):
        return []
    children = component.to_plotly_json()["props"].get("children")
    return _all_text(children) if children is not None else []


def test_render_header_shows_edit_button_and_description() -> None:
    header = render_header(_IDS, title="my-exp", description="does a thing")

    edit_button = _find_props(header, "edit-btn")
    assert edit_button is not None
    assert edit_button["n_clicks"] == 0

    assert "my-exp" in _all_text(header)
    assert "does a thing" in _all_text(header)


def test_render_header_modal_starts_closed_and_prefilled() -> None:
    header = render_header(_IDS, title="my-exp", description="does a thing")

    modal_props = _find_props(header, "modal")
    assert modal_props is not None
    assert modal_props["opened"] is False

    textarea_props = _find_props(header, "textarea")
    assert textarea_props is not None
    assert textarea_props["value"] == "does a thing"


def test_render_header_falls_back_to_placeholder_when_description_empty() -> None:
    header = render_header(_IDS, title="my-exp", description="")

    assert "No description" in _all_text(header)
