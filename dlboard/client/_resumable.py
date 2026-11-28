"""
What a sync and an import share, so that running either again goes on from where the last one stopped, however it did.

`_offline.sync` and `tensorboard.import_logdir` each keep:

- **Its state** (how far it read, and which runs it made) is written whole or not at all: a kill mid-write leaves the
  state before it, never a file that can't be read, which no later run could go on from.
- **A record the server refuses for good** costs itself, not what comes after it: it is skipped, with a warning, and the
  rest goes on. A failure the next run might not meet (the server or network down) or that every record would meet (the
  credentials, the run gone) stops this one instead, and the next starts again from that record.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from dlboard._batching import Failure
from dlboard.client._rest_api import classify_failure
from dlboard.client._warnings import warn

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from pydantic import BaseModel


def save(state: BaseModel, path: Path) -> None:
    """Write `state` to `path`, by way of a file beside it moved over it: whole, or the one before it."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(state.model_dump_json(indent=2))
    partial.replace(path)


@contextlib.contextmanager
def skipped_if_refused(what: str) -> Generator[None, None, None]:
    """Send `what` in the block; if the server refuses it for good (`Failure.PER_ITEM`), warn and go on without it."""
    try:
        yield
    except Exception as exc:
        if classify_failure(exc) is not Failure.PER_ITEM:
            raise
        warn(f"dlboard skipped {what}, which can never be sent: {exc}")
