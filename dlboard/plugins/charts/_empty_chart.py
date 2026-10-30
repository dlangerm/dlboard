"""What a chart says when the runs it draws logged nothing it reads."""

from __future__ import annotations

import dash_mantine_components as dmc


def nothing_logged(column: str) -> dmc.Stack:
    """An empty chart saying so, for a frame with no rows: none of the runs drawn logged `column` yet."""
    return dmc.Stack(
        [dmc.Text(f"Nothing logged for {column} by the runs shown.", c="dimmed", size="sm")], gap="xs"
    )
