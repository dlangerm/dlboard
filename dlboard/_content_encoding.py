"""
How a request body is compressed on its way to the server (#799): zstd where the client has it, else gzip.

Shared by the client, which compresses a JSON body it sends, and the server, which takes it apart. A metric batch is
mostly the same keys over and over: a flush of 50 steps of 12 keys went from 26 KB to 5.9 KB as zstd (4.4x, 0.05 ms) and
8.0 KB as gzip (3.2x, 0.16 ms), each at level 1, which also beat their default levels on size.

zstd is in the standard library from Python 3.14 (`compression.zstd`), and in `backports.zstd` before that: the
`dlboard-client[zstd]` extra, and always on the server. A client without either sends gzip, which every server that
takes compressed bodies takes.
"""

from __future__ import annotations

import gzip
import zlib
from typing import TYPE_CHECKING, Final

from dlboard._compat import StrEnum

if TYPE_CHECKING:
    # Pyrefly sees the backport, which this repo's own environment always has; the runtime looks for either.
    from backports import zstd
else:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        try:
            from backports import zstd
        except ImportError:
            zstd = None

_LEVEL: Final = 1


class ContentEncoding(StrEnum):
    """A request body's `Content-Encoding`, best first."""

    ZSTD = "zstd"
    GZIP = "gzip"


class TooLargeError(ValueError):
    """A body that decompresses to more than it may."""


def encodable() -> list[ContentEncoding]:
    """The encodings this Python can compress with, best first."""
    return [
        encoding for encoding in ContentEncoding if encoding is not ContentEncoding.ZSTD or zstd is not None
    ]


def encode(body: bytes, encoding: ContentEncoding) -> bytes:
    match encoding:
        case ContentEncoding.ZSTD:
            return zstd.compress(body, level=_LEVEL)
        case ContentEncoding.GZIP:
            return gzip.compress(body, compresslevel=_LEVEL)


def decode(body: bytes, encoding: ContentEncoding, limit: int) -> bytes:
    """
    `body` decompressed, if that is at most `limit` bytes: never more than one byte past it is ever made.

    Raises `TooLargeError` past it, and `ValueError` for a body that isn't whole, valid `encoding`.
    """
    match encoding:
        case ContentEncoding.ZSTD:
            decompressor = zstd.ZstdDecompressor()
            try:
                plain = decompressor.decompress(body, max_length=limit + 1)
            except zstd.ZstdError as error:
                raise ValueError(str(error)) from error
        case ContentEncoding.GZIP:
            decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            try:
                plain = decompressor.decompress(body, limit + 1)
            except zlib.error as error:
                raise ValueError(str(error)) from error
    if len(plain) > limit:
        msg = f"the body decompresses to more than {limit} bytes"
        raise TooLargeError(msg)
    if not decompressor.eof:
        msg = f"the body is not a whole {encoding} stream"
        raise ValueError(msg)
    return plain
