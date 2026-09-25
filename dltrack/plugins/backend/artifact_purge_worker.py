"""
Background worker that deletes purged artifacts' blobs from the `ArtifactStore`.

Signaled via `wake()` rather than polling the database on an interval: a `Dash` app has no
teardown hook to stop a polling thread cleanly, and polling would also make it impossible to
distinguish "a purge just happened in this process" from "leftover work from a previous, possibly
crashed, server run." Instead:

- The worker sits parked on `threading.Event.wait()` until `wake()` is called.
- A purge that runs in this process calls `wake()` itself (see `simple_admin_page.confirm_purge`),
  so newly-queued work is picked up automatically without any polling.
- A fresh server process starts with the event unset -- any `ArtifactPurgeTask` rows left over
  from a previous run (e.g. the process died mid-cleanup) just sit there until an admin explicitly
  clicks "Resume cleanup" in the admin page, which calls `wake()` too. This is deliberate: nothing
  here auto-resumes on startup, so a crash-loop or multiple server processes can't end up hammering
  the same artifact store concurrently.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from pydantic import AnyUrl
from structlog.stdlib import get_logger

from dltrack.serve import wait_for_artifact_store, wait_for_data_store

if TYPE_CHECKING:
    from dash import Dash

    from dltrack.models import ArtifactStore, DataStore

_log = get_logger(__name__)

_wake_event = threading.Event()


def wake() -> None:
    """Signal the worker to drain every currently-pending artifact-blob purge now."""
    _wake_event.set()


def _drain_pending(store: DataStore[...], artifact_store: ArtifactStore[...]) -> None:
    """
    Process every currently-pending purge task exactly once.

    Pure and synchronous -- no threading involved -- so it's directly unit-testable. Pages through
    `list_pending_artifact_purges` with an advancing `offset`, rather than re-fetching from the
    start until empty: a successfully-processed task's row is deleted, so it drops out of the
    table on its own, but a task that keeps failing stays pending -- re-fetching from offset 0
    every time would see that same task forever and never terminate. Advancing the offset by
    however many tasks in each page failed (and so remained) skips past them on the next fetch,
    so this always finishes after one pass over what was pending when it started.
    """
    offset = 0
    while True:
        batch = list(store.list_pending_artifact_purges(limit=100, offset=offset))
        if not batch:
            return
        still_pending = 0
        for task in batch:
            assert task.id is not None
            try:
                artifact_store.delete_artifact(AnyUrl(task.ref))
            except Exception as exc:  # noqa: BLE001 -- one bad ref must not stop the rest of the batch
                _log.warning("Failed to delete purged artifact blob %s: %s", task.ref, exc)
                store.fail_artifact_purge(task.id, str(exc))
                still_pending += 1
                continue
            store.complete_artifact_purge(task.id)
        offset += still_pending


def _run(app: Dash) -> None:
    store = wait_for_data_store(app)
    artifact_store = wait_for_artifact_store(app)
    while True:
        _wake_event.wait()
        _wake_event.clear()
        _drain_pending(store, artifact_store)


def plug(app: Dash) -> None:
    """Start the background artifact-blob purge worker."""
    threading.Thread(target=_run, args=(app,), daemon=True, name="artifact-purge-worker").start()
