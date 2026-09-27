"""The theme ships in the first response -- no callback round trip, no flash of an unthemed page."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dltrack.models import ColorScheme, SchemeColors, ThemeSpec
from dltrack.plugins import LOCAL_AUTH, LOCAL_STORAGE, themes
from dltrack.serve import app as build_app
from dltrack.serve import set_theme

if TYPE_CHECKING:
    from pathlib import Path

    from dash import Dash

    from dltrack.models import PluginProtocol


class _FollowTheOsTheme:
    """A third-party theme plugin: no brand color, just whatever scheme the viewer's OS uses."""

    @classmethod
    def plug(cls, app: Dash) -> None:
        set_theme(
            app,
            ThemeSpec(
                mantine={},
                default_color_scheme=ColorScheme.AUTO,
                page_background=SchemeColors(light="#fff", dark="#000"),
            ),
        )


@pytest.mark.parametrize(
    ("theme_plugins", "scheme", "primary_color"),
    [
        pytest.param([themes.default], ColorScheme.DARK, "brand", id="built-in"),
        pytest.param([_FollowTheOsTheme], ColorScheme.AUTO, None, id="custom"),
        pytest.param([], ColorScheme.LIGHT, None, id="no-theme-plugin"),
    ],
)
def test_the_theme_is_in_the_first_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    theme_plugins: list[PluginProtocol],
    scheme: ColorScheme,
    primary_color: str | None,
) -> None:
    monkeypatch.setenv("SQLITE_LOCATION", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("ARTIFACT_STORE_LOCATION", str(tmp_path / "artifacts"))
    client = build_app([*LOCAL_STORAGE, *LOCAL_AUTH, *theme_plugins]).server.test_client()

    index = client.get("/").get_data(as_text=True)
    provider = client.get("/_dash-layout").get_json()["props"]

    assert f'<script data-default-scheme="{scheme}">' in index
    assert provider["defaultColorScheme"] == scheme
    assert provider["theme"].get("primaryColor") == primary_color
