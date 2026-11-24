"""
"Log your first run": the empty state of every page that has nothing to show yet.

Instead of a bare "nothing here", an empty project list/project/experiment shows the exact
`DLBoardLogger` lines that would fill it -- already pointed at this server and at this project or
experiment, so getting a first run in is a copy-paste, not a trip to the docs.

On a server that checks who logs to it, a script needs an API key too: until the viewer has one, the card (and the
project list, once it has projects) makes them one in a click (`api_key_step`), shown once, beside the snippet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import dash_mantine_components as dmc
from dash import Dash, Input, Output, html
from dash.exceptions import PreventUpdate
from flask import request

from dlboard.serve import Icon, get_auth_provider, get_current_user, get_data_store, icon, mint_api_token
from dlboard.serve._backend._api_tokens import has_api_key
from dlboard.serve._component_ids import ButtonId, DivId
from dlboard.serve._pages._account_page import new_token_notice
from dlboard.serve._pages._dash_helpers import busy_while_running

if TYPE_CHECKING:
    from dash.development.base_component import Component


class _Onboarding:
    """Page tag: marks a component id as belonging to `_onboarding.py`."""


FIRST_KEY_ID: DivId[_Onboarding] = DivId("onboarding-first-key")
CREATE_FIRST_KEY_ID: ButtonId[_Onboarding] = ButtonId("onboarding-create-first-key")
FIRST_KEY_NAME: Final = "First key"
"""What the Account page lists the key `api_key_step` makes as."""

_API_KEY_HINT = "First set DLBOARD_API_KEY to a token from your Account page (top-right menu)."


def _server_url() -> str:
    """The URL the browser reached this server at (proxy path prefix included), for the client to use."""
    return request.url_root.rstrip("/")


def _snippet(constructor: str, *args: str) -> str:
    """The logger set up via `constructor(*args, server_url=...)`, one argument per line."""
    arg_lines = [f"    {arg}," for arg in [*args, f'server_url="{_server_url()}"']]
    return "\n".join(
        [
            "import pytorch_lightning as pl",
            "from dlboard.client.dlboard_logger import DLBoardLogger",
            "",
            f"logger = {constructor}(",
            *arg_lines,
            ")",
            "trainer = pl.Trainer(logger=logger)",
        ]
    )


def first_project_snippet() -> str:
    """Create a project (and experiment) by name on first use."""
    return _snippet("DLBoardLogger.from_names", '"my-project"')


def first_experiment_snippet(project_id: int) -> str:
    """Log into this project; the logger creates an experiment for the run."""
    return _snippet("DLBoardLogger", f"project_id={project_id}")


def first_run_snippet(project_id: int, experiment_id: int) -> str:
    """Log runs into this experiment."""
    return _snippet("DLBoardLogger", f"project_id={project_id}", f"experiment_id={experiment_id}")


def api_key_step() -> Component | None:
    """
    The viewer's first API key, a click away, on a server that checks who logs to it.

    `None` on a server that doesn't, or for a viewer who has a key already: the step is shown until they have one.
    """
    if not get_auth_provider().verifies_identity or has_api_key(get_data_store(), get_current_user()):
        return None
    return html.Div(
        dmc.Alert(
            dmc.Group(
                [
                    dmc.Text(
                        "Your training scripts log in with an API key, and you don't have one yet.", size="sm"
                    ),
                    dmc.Button(
                        "Create my API key", id=CREATE_FIRST_KEY_ID, leftSection=icon(Icon.KEY), size="xs"
                    ),
                ],
                justify="space-between",
                gap="sm",
            ),
            title="First, an API key",
            icon=icon(Icon.KEY),
            variant="light",
        ),
        id=FIRST_KEY_ID,
    )


def onboarding_card(*, title: str, snippet: str) -> dmc.Paper:
    """`title`, a one-line pointer to the `DLBoardLogger`, the viewer's first API key if they need one, and `snippet`."""
    key_step = api_key_step()
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
                            # A server that verifies identity needs the script to authenticate too, with the key the
                            # viewer has already, or the one `key_step` makes them.
                            *(
                                [dmc.Text(_API_KEY_HINT, size="sm", c="dimmed")]
                                if key_step is None and get_auth_provider().verifies_identity
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
            *([dmc.Box(key_step, mt="md")] if key_step is not None else []),
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


def register(app: Dash) -> None:
    """Make the viewer their first API key, and show it in place of the button, the one time it is shown."""

    @app.callback(
        Output(FIRST_KEY_ID, "children"),
        Input(CREATE_FIRST_KEY_ID, "n_clicks"),
        running=busy_while_running(CREATE_FIRST_KEY_ID),  # one key a click, however fast the clicks
        prevent_initial_call=True,
    )
    def create_first_key(n_clicks: int | None) -> Component:
        if not n_clicks:
            raise PreventUpdate
        _token, raw = mint_api_token(get_data_store(), get_current_user(), FIRST_KEY_NAME)
        return new_token_notice(raw)
