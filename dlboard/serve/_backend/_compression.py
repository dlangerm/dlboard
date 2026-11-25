"""
Compressing what the server sends (#798): zstd where the browser takes it, else Brotli, else gzip.

A reverse proxy in front usually compresses, but a server serving HTTPS itself (#797) has none, and what an experiment
page downloads is mostly figure JSON (long numeric arrays, repeated keys) that compresses several times over: a page of
seven line charts of eight 2,000-step runs came to 5.6 MB, and 1.0 MB as zstd. `flask-compress` negotiates the encoding
from `Accept-Encoding`, and compresses only text-like types: a logged picture or archive, already compressed, is sent as
it is.

Each encoding runs at its fastest level. On that page that was also the best trade: zstd took 15 ms at level 1 (5.6x
smaller) and 26 ms at its default 3 (5.0x); Brotli 17 ms at 1 (5.4x) and 85 ms at its default 4; gzip, all a browser
offers over plain HTTP, 39 ms at 1 (4.4x) and 154 ms at its default 6.

A client on this same machine gets nothing compressed: moving bytes over loopback costs nothing, so compressing them
would only add the CPU time to every page. Behind a reverse proxy, that is the client the proxy says it is
(`DLBOARD_TRUSTED_PROXIES`).
"""

from __future__ import annotations

from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING, Final

from flask import request
from flask_compress import Compress
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from dash import Dash
    from flask import Response

_CONFIG: Final = {
    # Best first: of the ones a browser offers alike, the first is used.
    "COMPRESS_ALGORITHM": ["zstd", "br", "gzip"],
    "COMPRESS_ZSTD_LEVEL": 1,
    "COMPRESS_BR_LEVEL": 1,
    "COMPRESS_LEVEL": 1,  # gzip's
    "COMPRESS_REGISTER": False,  # `install_compression` decides which responses it sees
}


class CompressionSettings(BaseSettings):
    """`DLBOARD_COMPRESS_RESPONSES`, off for a deployment whose proxy compresses already: twice wastes CPU for nothing."""

    model_config = SettingsConfigDict(env_prefix="DLBOARD_")

    compress_responses: bool = True


def _on_this_machine(address: str | None) -> bool:
    if address is None:
        return False
    parsed = ip_address(address)
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def install_compression(app: Dash) -> None:
    """Compress `app`'s responses, unless switched off. Its own `after_request`, so request logging times it too."""
    if not CompressionSettings().compress_responses:
        return
    app.server.config.update(_CONFIG)
    compress = Compress(app.server)

    @app.server.after_request
    def _compress(response: Response) -> Response:
        return response if _on_this_machine(request.remote_addr) else compress.after_request(response)
