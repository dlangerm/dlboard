# pyright: reportPrivateUsage=false
"""Tests for the shared `_delete_confirm.py` button+modal markup."""

from __future__ import annotations

from typing import Any, cast

from dltrack.plugins.pages._delete_confirm import DeleteConfirmIds, render_delete_control

_IDS = DeleteConfirmIds(button="btn", modal="modal", confirm="confirm", cancel="cancel")


def test_render_delete_control_returns_a_button_and_a_closed_modal() -> None:
    button, modal = (
        cast("Any", c) for c in render_delete_control(_IDS, label="Delete project", entity_noun="project")
    )

    assert button.id == "btn"
    assert button.children == "Delete project"
    assert modal.id == "modal"
    assert modal.opened is False


def test_render_delete_control_modal_mentions_the_entity_noun() -> None:
    _button, modal = (
        cast("Any", c) for c in render_delete_control(_IDS, label="Delete run", entity_noun="run")
    )

    assert "run" in modal.title
