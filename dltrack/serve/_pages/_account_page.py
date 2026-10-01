"""The account page: who you're signed in as, and the API tokens your training scripts log in with."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import dash_mantine_components as dmc
from dash import ALL, Dash, Input, Output, State, html, no_update

from dltrack.models import ButtonId, DivId, ValueId, constants
from dltrack.serve import Icon, get_auth_provider, get_current_user, get_data_store, icon, mint_api_token
from dltrack.serve._pages._dash_helpers import require_triggered_id, section_label

if TYPE_CHECKING:
    from dash.development.base_component import Component

    from dltrack.models import ApiToken


class _AccountPage:
    """Page tag: marks a component id as belonging to `_account_page.py`."""


PAGE_ACCOUNT_ID: DivId[_AccountPage] = DivId("account-container")
TOKEN_NAME_ID: ValueId[_AccountPage] = ValueId("account-token-name")
CREATE_TOKEN_ID: ButtonId[_AccountPage] = ButtonId("account-create-token")
NEW_TOKEN_ID: DivId[_AccountPage] = DivId("account-new-token")
# Pattern-matched "type" discriminator, not a standalone component id -- a plain string.
REVOKE_TOKEN_TYPE: Final = "account-revoke-token"


def _when(token_time: object) -> str:
    return f"{token_time:%Y-%m-%d %H:%M} UTC" if token_time else "-"


def _token_row(token: ApiToken) -> Component:
    return dmc.TableTr(
        [
            dmc.TableTd(dmc.Text(token.name, fw=600, size="sm")),
            dmc.TableTd(dmc.Code(f"dlt_{token.token_id}_…")),
            dmc.TableTd(_when(token.created_at)),
            dmc.TableTd(_when(token.last_used_at)),
            dmc.TableTd(
                dmc.Badge("Revoked", color="red", variant="light", size="sm")
                if token.revoked_at
                else dmc.Button(
                    "Revoke",
                    id={"type": REVOKE_TOKEN_TYPE, "token": token.id},
                    size="xs",
                    color="red",
                    variant="light",
                )
            ),
        ]
    )


def new_token_notice(raw_token: str) -> Component:
    """The only time a token's secret is ever shown, with how to use it."""
    snippet = f"export DLTRACK_API_KEY={raw_token}"
    return dmc.Alert(
        [
            dmc.Text("Copy this token now -- it won't be shown again.", size="sm", mb="xs"),
            dmc.Group(
                [
                    dmc.Code(snippet, block=True, style={"flex": 1, "wordBreak": "break-all"}),
                    dmc.CopyButton(
                        value=snippet,
                        children=[icon(Icon.COPY), " Copy"],
                        copiedChildren=[icon(Icon.COPIED), " Copied"],
                        color="gray",
                        copiedColor="green",
                        variant="subtle",
                        size="compact-xs",
                    ),
                ],
                wrap="nowrap",
            ),
        ],
        color="green",
        variant="light",
    )


def render_account_page() -> Component:
    """The whole account page, for whoever's signed in."""
    user, provider = get_current_user(), get_auth_provider()
    tokens = get_data_store().list_api_tokens(user.id)
    return dmc.Container(
        [
            dmc.Title("Account", order=2, fw=700),
            dmc.Text(f"Signed in as {user.username} via {provider.display_name}", c="dimmed", mb="xl"),
            section_label("API tokens"),
            dmc.Text(
                "A training script authenticates with a token: set it as DLTRACK_API_KEY wherever "
                "DLTrackLogger runs. It acts as you, with access to everything you can access.",
                size="sm",
                c="dimmed",
                mb="sm",
            ),
            dmc.Group(
                [
                    dmc.TextInput(
                        id=TOKEN_NAME_ID,
                        placeholder="What it's for, e.g. cluster jobs",
                        w=280,
                        **cast("dict[str, Any]", {"aria-label": "Token name"}),
                    ),
                    dmc.Button("Create token", id=CREATE_TOKEN_ID, leftSection=icon(Icon.KEY)),
                ],
                gap="xs",
                align="flex-start",
                mb="sm",
            ),
            html.Div(id=NEW_TOKEN_ID),
            dmc.Table(
                [
                    dmc.TableThead(
                        dmc.TableTr([dmc.TableTh(h) for h in ("Name", "Token", "Created", "Last used", "")])
                    ),
                    dmc.TableTbody([_token_row(t) for t in tokens]),
                ],
                withTableBorder=True,
                verticalSpacing="xs",
                mt="md",
            )
            if tokens
            else dmc.Text("No tokens yet.", size="sm", c="dimmed", mt="md"),
        ],
        size="md",
        py="xl",
    )


def register(app: Dash) -> None:
    """Wire the account page's token controls."""

    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(NEW_TOKEN_ID, "children"),
        Output(TOKEN_NAME_ID, "value"),
        Output(TOKEN_NAME_ID, "error"),
        Input(CREATE_TOKEN_ID, "n_clicks"),
        Input(TOKEN_NAME_ID, "n_submit"),
        State(TOKEN_NAME_ID, "value"),
        prevent_initial_call=True,
    )
    def create_token(_clicks: int, _submits: int, name: str | None) -> tuple[Any, Any, str | None]:
        if not name or not name.strip():
            return no_update, no_update, "Name the token"
        _token, raw = mint_api_token(get_data_store(), get_current_user(), name.strip())
        return new_token_notice(raw), "", None

    # Hard-reloads rather than re-rendering the table in place: the table holds the very
    # pattern-matched (`ALL`) Revoke buttons this callback listens to (see the admin page's trash).
    @app.callback(  # pyright: ignore[reportUnknownMemberType]
        Output(constants.LOCATION_ID, "href", allow_duplicate=True),
        Output(constants.LOCATION_ID, "refresh", allow_duplicate=True),
        Input({"type": REVOKE_TOKEN_TYPE, "token": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )
    def revoke_token(_clicks: list[int]) -> tuple[str, bool]:
        token_id = int(cast("dict[str, int]", require_triggered_id())["token"])
        get_data_store().revoke_api_token(token_id, get_current_user().id)
        return "/account", True
