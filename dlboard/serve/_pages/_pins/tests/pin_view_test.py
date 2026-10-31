"""How a pin is drawn: in its colour, and never as the colour's name."""

from __future__ import annotations

import pendulum
import pytest

from dlboard.models import NamedPin, PinColor, RunPin
from dlboard.serve._pages._pins._pin_menus import pin_menus
from dlboard.serve._pages._pins._pin_view import pin_cells, pin_css_color, pin_glyph
from dlboard.serve._pages._run_list import RunListSurface

_TS = pendulum.datetime(2026, 1, 1, tz="UTC")


def _pin(pin_id: int, color: PinColor, by: int, username: str) -> NamedPin:
    pin = RunPin(
        id=pin_id, run_id=1, experiment_id=1, pinned_by=by, color=color, pinned_at=_TS.add(seconds=pin_id)
    )
    return NamedPin(pin=pin, username=username)


def test_a_run_pinned_in_a_colour_by_several_people_has_one_cell_in_that_colour_naming_them() -> None:
    pins = [
        _pin(1, PinColor.GREEN, 2, "bob"),
        _pin(2, PinColor.RED, 1, "alice"),
        _pin(3, PinColor.RED, 2, "bob"),
    ]

    cells = pin_cells(pins, me=1)

    assert [(cell.color, cell.who, cell.mine) for cell in cells] == [
        (PinColor.RED, ["alice", "bob"], True),
        (PinColor.GREEN, ["bob"], False),
    ]


def test_a_plain_pin_is_not_the_colour_of_a_gray_one() -> None:
    assert pin_css_color(PinColor.PLAIN) != pin_css_color(PinColor.GRAY)
    assert {pin_css_color(color) for color in PinColor} >= {"var(--mantine-color-red-6)"}
    assert len({pin_css_color(color) for color in PinColor}) == len(PinColor)


def _texts(node: object) -> list[str]:
    """Every piece of text a component shows: the strings among its children, and its descendants'."""
    if isinstance(node, str):
        return [node]
    children: object = getattr(node, "children", None)
    items: list[object] = children if isinstance(children, list) else [] if children is None else [children]
    return [text for child in items for text in _texts(child)]


@pytest.mark.parametrize("surface", list(RunListSurface))
def test_no_colour_is_ever_named_in_text_a_pin_shows(surface: RunListSurface) -> None:
    shown = [text.lower() for menu in pin_menus(surface) for text in _texts(menu)]
    shown += [text.lower() for color in PinColor for text in _texts(pin_glyph(color))]

    assert not [
        text for text in shown if any(color.value in text for color in PinColor if color.value != "plain")
    ]
