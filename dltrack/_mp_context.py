"""
Shared multiprocessing context for dltrack's background worker processes.

`DLTrackLogger` (client-side metric/artifact shipping) and `FSArtifactStore` (server-side blob
writes) each spawn a worker process off a `multiprocessing.Queue`; both need the same "spawn"
context (never "fork", which can deadlock a process that already has threads running -- both of
these do, e.g. Flask's/Dash's request-handling threads on the server side). Sharing one context
instance keeps that choice in exactly one place instead of two independent literal `"spawn"` calls.
"""

from __future__ import annotations

from multiprocessing import get_context

SPAWN_CONTEXT = get_context("spawn")
