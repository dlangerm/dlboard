"""Every page's CSP lets it load pictures, media, fonts and data from dlboard alone, and what a plugin allows (#622)."""

from __future__ import annotations

import pytest
from dash import Dash

from dlboard.serve._backend._auth import (
    RESOURCE_ORIGINS,
    ResourceOrigins,
    allow_resources_from,
    allow_resources_from_anywhere,
    content_security_policy,
)


def test_a_page_loads_from_dlboard_alone_by_default_and_is_never_framed() -> None:
    assert content_security_policy(None) == (
        "frame-ancestors 'none'; img-src 'self' data: blob:; media-src 'self' data: blob:; font-src 'self' data:; "
        "connect-src 'self' data: blob:"
    )


def test_a_plugin_adds_an_origin_or_lets_a_page_load_from_anywhere() -> None:
    app = Dash(__name__)

    allow_resources_from(app, "https://tiles.example.com/{z}/{x}/{y}.png")
    allow_resources_from(app, "http://minio:9000/bucket/key?X-Amz-Signature=abc")

    origins = RESOURCE_ORIGINS.get(app)
    assert origins == ResourceOrigins(origins={"https://tiles.example.com", "http://minio:9000"})
    assert (
        "img-src 'self' data: blob: http://minio:9000 https://tiles.example.com;"
        in content_security_policy(origins)
    )
    allow_resources_from_anywhere(app)
    assert content_security_policy(RESOURCE_ORIGINS.get(app)) == "frame-ancestors 'none'"


@pytest.mark.parametrize("url", ["tiles.example.com/{z}", "file:///etc/passwd", "javascript:alert(1)"])
def test_only_an_http_origin_can_be_allowed(url: str) -> None:
    with pytest.raises(ValueError, match="not an http"):
        allow_resources_from(Dash(__name__), url)
