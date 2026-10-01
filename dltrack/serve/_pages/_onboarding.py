"""
"Log your first run": the empty state of every page that has nothing to show yet.

Instead of a bare "nothing here", an empty project list/project/experiment shows the exact
`DLTrackLogger` lines that would fill it -- already pointed at this server and at this project or
experiment, so getting a first run in is a copy-paste, not a trip to the docs.
"""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import html
from flask import request

from dltrack.serve import Icon, get_auth_provider, icon

_API_KEY_HINT = "First set DLTRACK_API_KEY to a token from your Account page (top-right menu)."


def _server_url() -> str:
    """The URL the browser reached this server at (proxy path prefix included), for the client to use."""
    return request.url_root.rstrip("/")


def _snippet(constructor: str, *args: str) -> str:
    """The logger set up via `constructor(*args, server_url=...)`, one argument per line."""
    arg_lines = [f"    {arg}," for arg in [*args, f'server_url="{_server_url()}"']]
    return "\n".join(
        [
            "import pytorch_lightning as pl",
            "from dltrack.client import DLTrackLogger",
            "",
            f"logger = {constructor}(",
            *arg_lines,
            ")",
            "trainer = pl.Trainer(logger=logger)",
        ]
    )


def first_project_snippet() -> str:
    """Create a project (and experiment) by name on first use."""
    return _snippet("DLTrackLogger.from_names", '"my-project"')


def first_experiment_snippet(project_id: int) -> str:
    """Log into this project; the logger creates an experiment for the run."""
    return _snippet("DLTrackLogger", f"project_id={project_id}")


def first_run_snippet(project_id: int, experiment_id: int) -> str:
    """Log runs into this experiment."""
    return _snippet("DLTrackLogger", f"project_id={project_id}", f"experiment_id={experiment_id}")


def onboarding_card(*, title: str, snippet: str) -> dmc.Paper:
    """`title`, a one-line pointer to the `DLTrackLogger`, and `snippet` with a copy button."""
    return dmc.Paper(
        [
            dmc.Group(
                [
                    html.Span(icon(Icon.LOGO), className="dl-logo"),
                    dmc.Stack(
                        [
                            dmc.Text(title, fw=600),
                            dmc.Text(
                                "Point the PyTorch Lightning logger at this server — it's already filled in:",
                                size="sm",
                                c="dimmed",
                            ),
                            # A server that verifies identity needs the script to authenticate too.
                            *(
                                [dmc.Text(_API_KEY_HINT, size="sm", c="dimmed")]
                                if get_auth_provider().verifies_identity
                                else []
                            ),
                        ],
                        gap=2,
                    ),
                ],
                gap="sm",
                wrap="nowrap",
                align="flex-start",
            ),
            dmc.Box(
                [
                    dmc.Code(snippet, block=True, className="dl-snippet"),
                    dmc.CopyButton(
                        value=snippet,
                        children=[icon(Icon.COPY), " Copy"],
                        copiedChildren=[icon(Icon.COPIED), " Copied"],
                        color="gray",
                        copiedColor="green",
                        variant="subtle",
                        size="compact-xs",
                        className="dl-snippet-copy",
                    ),
                ],
                pos="relative",
                mt="md",
            ),
        ],
        withBorder=True,
        radius="md",
        p="lg",
        maw=720,
        mx="auto",
        className="dl-onboarding",
    )
