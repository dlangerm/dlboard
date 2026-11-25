"""
Compression both ways: what the server sends (#798), and the request bodies a client compressed (#799).

**Responses**: zstd where the browser takes it, else Brotli, else gzip. A reverse proxy in front usually compresses, but a
server serving HTTPS itself (#797) has none, and what an experiment page downloads is mostly figure JSON (long numeric
arrays, repeated keys) that compresses several times over: a page of seven line charts of eight 2,000-step runs came to
5.6 MB, and 1.0 MB as zstd. `flask-compress` negotiates the encoding from `Accept-Encoding`, and compresses only
text-like types: a logged picture or archive, already compressed, is sent as it is.

Each encoding runs at its fastest level. On that page that was also the best trade: zstd took 15 ms at level 1 (5.6x
smaller) and 26 ms at its default 3 (5.0x); Brotli 17 ms at 1 (5.4x) and 85 ms at its default 4; gzip, all a browser
offers over plain HTTP, 39 ms at 1 (4.4x) and 154 ms at its default 6.

A client on this same machine gets nothing compressed: moving bytes over loopback costs nothing, so compressing them
would only add the CPU time to every page. Behind a reverse proxy, that is the client the proxy says it is
(`DLBOARD_TRUSTED_PROXIES`).

**Request bodies** a client compressed (`ContentEncoding`) are decompressed before a route reads them, once the gate has let
the request through, so no one it turns away can make the server decompress anything. What a body decompresses to is
held to `DLBOARD_MAX_UPLOAD_MB` like any other body (413 past it), and an encoding this server doesn't take is a 415.
`whoami` lists the encodings, so a client never sends one to a server that would refuse it.
"""

from __future__ import annotations

from http import HTTPStatus
from io import BytesIO
from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING, Final

from flask import abort, request
from flask_compress import Compress
from pydantic_settings import BaseSettings, SettingsConfigDict
from werkzeug.wsgi import get_input_stream

from dlboard._compat import StrEnum
from dlboard._content_encoding import ContentEncoding, TooLargeError, decompress
from dlboard.serve._backend._auth import RequestLimits

if TYPE_CHECKING:
    from dash import Dash
    from flask import Response


class ResponseEncoding(StrEnum):
    """A response's `Content-Encoding`, best first: of the ones a browser offers alike, the first is used."""

    ZSTD = "zstd"
    BR = "br"
    GZIP = "gzip"


_CONFIG: Final = {
    "COMPRESS_ALGORITHM": list(ResponseEncoding),
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
    """
    Whether the client is on this machine, by its address: one that isn't an address isn't.

    Behind a proxy, `ProxyFix` puts what `X-Forwarded-For` says here, which some proxies send as `unknown`.
    """
    try:
        parsed = ip_address(address or "")
    except ValueError:
        return False
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def _decompress_body() -> None:
    """
    Put a compressed request body back as the plain one, for whatever reads it next.

    Swapped in under the WSGI environ, which `request.stream` is made from the first time anything reads the body: the
    gate and the request log only ever read headers, so nothing has yet.
    """
    named = request.headers.get("Content-Encoding")
    if not named:
        return
    if named.strip().lower() not in set(ContentEncoding):
        abort(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
    limit = RequestLimits().max_upload_bytes
    try:
        body = decompress(get_input_stream(request.environ, max_content_length=limit).read(), limit)
    except TooLargeError:
        abort(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    except ValueError:
        abort(HTTPStatus.BAD_REQUEST)
    request.environ.update({"wsgi.input": BytesIO(body), "CONTENT_LENGTH": str(len(body))})
    request.environ.pop("HTTP_CONTENT_ENCODING")


def install_compression(app: Dash) -> None:
    """
    Take compressed request bodies, and compress `app`'s responses unless switched off.

    Installed after the request gate, so its `before_request` runs after the gate's, and its `after_request` first, so
    that the request log times compressing the response too (Flask runs those last-registered first).
    """
    app.server.before_request(_decompress_body)
    if not CompressionSettings().compress_responses:
        return
    app.server.config.update(_CONFIG)
    compress = Compress(app.server)

    @app.server.after_request
    def _compress(response: Response) -> Response:
        return response if _on_this_machine(request.remote_addr) else compress.after_request(response)
