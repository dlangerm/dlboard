"""
Backports for stdlib `typing`/`enum` members not available on the client's floor (Python 3.10).

Nothing here is dltrack-specific; it exists only because the client supports 3.10+ while the
syntax/stdlib members below landed later. Every model/client module that needs one of these
imports it from here instead of `enum` directly, so there's exactly one place that cares which
Python version is actually running.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Pyright always sees the real `enum.StrEnum` -- this repo's own dev/CI env always has 3.12+.
    # The try/except below is the runtime-only fallback, for an actual 3.10/3.11 interpreter.
    from enum import StrEnum
else:
    try:
        from enum import StrEnum
    except ImportError:  # Python < 3.11 (PEP 663 landed in 3.11)
        from enum import Enum

        class StrEnum(str, Enum):
            """A `str`-valued `Enum` whose `str()` is its value, not `ClassName.MEMBER`."""

            def __str__(self) -> str:
                return str(self.value)


__all__ = ["StrEnum"]
