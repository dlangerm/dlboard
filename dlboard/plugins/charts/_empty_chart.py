"""What a chart says when the runs it draws logged nothing it reads."""

from __future__ import annotations

import dash_mantine_components as dmc


def nothing_to_place(column: str, x_axis: str) -> dmc.Stack:
    """An empty chart saying so, for rows none of which has an `x_axis` value the axis can place (no date, say)."""
    return dmc.Stack(
        [
            dmc.Text(
                f"None of {column}'s values has an {x_axis} the axis can place: try another x-axis or x-axis type.",
                c="dimmed",
                size="sm",
            )
        ],
        gap="xs",
    )


def nothing_logged(column: str) -> dmc.Stack:
    """An empty chart saying so, for a frame with no rows: none of the runs drawn logged `column` yet."""
    return dmc.Stack(
        [dmc.Text(f"Nothing logged for {column} by the runs shown.", c="dimmed", size="sm")], gap="xs"
    )
