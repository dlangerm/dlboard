"""Which kind of server process this is: a web worker (or a test), or the custodian, which serves no request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass
class _Role:
    custodian: bool = False


_ROLE: Final = _Role()


def in_custodian() -> bool:
    """
    Whether this process is the custodian: a plugin starts nothing it only needs to serve requests here.

    The custodian builds the app as a web worker does, for its stores and its plugins' tasks, but takes no upload: its
    artifact store has no blob writer, and no ref-ingest thread.
    """
    return _ROLE.custodian


def become_custodian() -> None:
    """Mark this process as the custodian, before it builds the app."""
    _ROLE.custodian = True
