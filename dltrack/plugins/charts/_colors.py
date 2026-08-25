"""Deterministic per-run color assignment shared by charts that plot one series per run."""

from hashlib import md5

_COLORS = [
    "gray",
    "red",
    "pink",
    "grape",
    "violet",
    "indigo",
    "blue",
    "cyan",
    "teal",
    "green",
    "lime",
    "yellow",
    "orange",
]


def hash_color(run_id: int, temperature: int = 5) -> str:
    """Pick a stable Mantine color/shade for a run id, so a run keeps the same color across charts."""
    v = int(md5(str(run_id).encode(), usedforsecurity=False).hexdigest(), base=16) % len(_COLORS)
    return _COLORS[v] + f".{temperature % 10}"
