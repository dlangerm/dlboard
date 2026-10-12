"""What an artifact tag can hold, and how it is shown."""

from __future__ import annotations

from typing import Final, TypeAlias

from pydantic import BaseModel

from dlboard._compat import StrEnum


class Unit(StrEnum):
    """
    A unit a `Quantity` is measured in, so it is shown in a readable multiple (`1.5 h`, not `5400 s`).

    Stored as the value (the symbol) inside a tag, so a value is never renamed or removed.
    """

    MILLISECONDS = "ms"
    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"
    DAYS = "d"
    BYTES = "B"
    KILOBYTES = "kB"
    MEGABYTES = "MB"
    GIGABYTES = "GB"
    TERABYTES = "TB"


_TIME: Final = (Unit.MILLISECONDS, Unit.SECONDS, Unit.MINUTES, Unit.HOURS, Unit.DAYS)
_SIZE: Final = (Unit.BYTES, Unit.KILOBYTES, Unit.MEGABYTES, Unit.GIGABYTES, Unit.TERABYTES)
"""Each family, smallest to largest."""

_BASE_PER_UNIT: Final = {
    Unit.MILLISECONDS: 1e-3,
    Unit.SECONDS: 1.0,
    Unit.MINUTES: 60.0,
    Unit.HOURS: 3600.0,
    Unit.DAYS: 86400.0,
    Unit.BYTES: 1.0,
    Unit.KILOBYTES: 1e3,
    Unit.MEGABYTES: 1e6,
    Unit.GIGABYTES: 1e9,
    Unit.TERABYTES: 1e12,
}
"""How many seconds (or bytes) one of each unit is."""


class Quantity(BaseModel, frozen=True):
    """A number with a unit, e.g. `Quantity(value=2.5, unit=Unit.GIGABYTES)`."""

    value: float
    unit: Unit


TagValue: TypeAlias = str | int | float | bool | Quantity
"""What a caller may put in a tag: a JSON scalar or a `Quantity`, shown by `format_tag_value`."""

_FLOAT_FORMAT: Final = "#.4g"
"""Four significant digits, trailing zeros kept, so a column of scores lines up (`0.1410`, `0.1438`)."""

_THOUSANDS_FROM: Final = 1e3
_EXPONENT_FROM: Final = 1e15


def _format_float(value: float) -> str:
    """Up to a quadrillion written out with separators (`12,346`), where `g` would give `1.235e+04`."""
    if _THOUSANDS_FROM <= abs(value) < _EXPONENT_FROM:
        return f"{value:,.0f}"
    return format(value, _FLOAT_FORMAT)


def _format_quantity(quantity: Quantity) -> str:
    """In the largest unit of its family the value is at least one of (`5400 s` is `1.500 h`)."""
    family = _TIME if quantity.unit in _TIME else _SIZE
    base = quantity.value * _BASE_PER_UNIT[quantity.unit]
    if base == 0:
        return f"0 {quantity.unit}"
    unit = max(
        (u for u in family if abs(base) >= _BASE_PER_UNIT[u]),
        key=_BASE_PER_UNIT.__getitem__,
        default=family[0],
    )
    return f"{_format_float(base / _BASE_PER_UNIT[unit])} {unit}"


def format_tag_value(value: TagValue) -> str:
    """`value` as shown next to an artifact, formatted the same way whoever logged it."""
    match value:
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            return _format_float(value)
        case str():
            return value
        case Quantity():
            return _format_quantity(value)
