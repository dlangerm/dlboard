"""What an artifact tag can hold, and how it is shown."""

from __future__ import annotations

from typing import Final, TypeAlias

from pydantic import BaseModel, Field

from dlboard._compat import StrEnum


class TimeUnit(StrEnum):
    """
    A unit of time, smallest to largest. Stored as the symbol inside a tag, so a value is never renamed or removed.

    What a client says its number is *measured in*; how it is shown is up to the display (`format_tag_value`).
    """

    NANOSECONDS = "ns"
    MICROSECONDS = "us"
    MILLISECONDS = "ms"
    SECONDS = "s"
    MINUTES = "min"
    HOURS = "h"
    DAYS = "d"


class SizeUnit(StrEnum):
    """A unit of data size (decimal), smallest to largest. Stored as the symbol, like `TimeUnit`."""

    BYTES = "B"
    KILOBYTES = "kB"
    MEGABYTES = "MB"
    GIGABYTES = "GB"
    TERABYTES = "TB"


Unit: TypeAlias = TimeUnit | SizeUnit

_BASE_PER_UNIT: Final[dict[Unit, float]] = {
    TimeUnit.NANOSECONDS: 1e-9,
    TimeUnit.MICROSECONDS: 1e-6,
    TimeUnit.MILLISECONDS: 1e-3,
    TimeUnit.SECONDS: 1.0,
    TimeUnit.MINUTES: 60.0,
    TimeUnit.HOURS: 3600.0,
    TimeUnit.DAYS: 86400.0,
    SizeUnit.BYTES: 1.0,
    SizeUnit.KILOBYTES: 1e3,
    SizeUnit.MEGABYTES: 1e6,
    SizeUnit.GIGABYTES: 1e9,
    SizeUnit.TERABYTES: 1e12,
}
"""How many seconds (or bytes) one of each unit is."""


class Quantity(BaseModel, frozen=True):
    """A number and the unit it was measured in, e.g. `Quantity(value=2.5, unit=SizeUnit.GIGABYTES)`."""

    value: float
    unit: Unit

    @property
    def base_value(self) -> float:
        """`value` in seconds (a duration) or bytes (a size), so quantities compare whatever unit each was logged in."""
        return self.value * _BASE_PER_UNIT[self.unit]

    @property
    def base_unit(self) -> Unit:
        """The unit `base_value` is in."""
        return TimeUnit.SECONDS if isinstance(self.unit, TimeUnit) else SizeUnit.BYTES


TagValue: TypeAlias = str | int | float | bool | Quantity
"""What a caller may put in a tag: a JSON scalar or a `Quantity`, shown by `format_tag_value`."""

DEFAULT_TAG_PRECISION: Final = 4
"""Significant digits a number is shown with unless the display says otherwise."""


class TagDisplay(BaseModel, frozen=True, extra="forbid"):
    """
    How a tag's number is shown, which is up to the display and never the client. `None` leaves it as it is.

    A chart's own `TagDisplay` is the default for all its tags; one for a particular tag key overrides it
    (`overridden_by`).
    """

    precision: int | None = Field(default=None, ge=1, le=15, description="Significant digits.")
    time_unit: TimeUnit | None = Field(
        default=None, description="Unit to show a duration in, whatever it was logged in."
    )
    size_unit: SizeUnit | None = Field(
        default=None, description="Unit to show a data size in, whatever it was logged in."
    )

    def overridden_by(self, other: TagDisplay) -> TagDisplay:
        """A copy of this with every field `other` sets taking its value."""
        return self.model_copy(update=other.model_dump(exclude_none=True))


_EXPONENT_FROM: Final = 1e15


def _format_float(value: float, precision: int) -> str:
    """
    `value` to `precision` significant digits, trailing zeros kept so a column of scores lines up (`0.1410`).

    From one up to a quadrillion it is written out, with separators (`12,346`), where `g` would give
    `1.235e+04`, or `90.` for `90` at two digits.
    """
    if 1 <= abs(value) < _EXPONENT_FROM:
        return f"{value:,.{max(0, precision - len(str(int(abs(value)))))}f}"
    return format(value, f"#.{precision}g")


def format_tag_value(value: TagValue, display: TagDisplay = TagDisplay()) -> str:  # noqa: B008
    """
    `value` as shown next to an artifact, formatted the same way whoever logged it.

    A `Quantity` is converted from the unit its client measured it in to `display.time_unit` /
    `display.size_unit` (whichever matches its kind), or, if that is `None`, to the largest unit of its
    kind it is at least one of (`5400 s` is `1.500 h`). `display.precision` is the number of significant
    digits.
    """
    precision = display.precision or DEFAULT_TAG_PRECISION
    match value:
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            return _format_float(value, precision)
        case str():
            return value
        case Quantity():
            base = value.base_value
            unit = (display.time_unit if isinstance(value.unit, TimeUnit) else display.size_unit) or max(
                (u for u in type(value.unit) if abs(base) >= _BASE_PER_UNIT[u]),
                key=_BASE_PER_UNIT.__getitem__,
                default=value.unit if base == 0 else next(iter(type(value.unit))),
            )
            return f"{_format_float(base / _BASE_PER_UNIT[unit], precision)} {unit}"
