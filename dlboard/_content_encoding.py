"""
How a request body is compressed on its way to the server (#799): as zstd.

Shared by the client, which compresses a JSON body it sends, and the server, which takes it apart. A metric batch is
mostly the same keys over and over: a flush of 50 steps of 12 keys went from 26 KB to 5.9 KB as zstd at level 1 (4.4x,
0.05 ms), which also beat its default level on size; gzip made 8.0 KB, three times slower.

zstd is in the standard library from Python 3.14 (`compression.zstd`), and in `backports.zstd`, a dependency of both
distributions, before that.
"""

from __future__ import annotations

import sys
from typing import Final

from dlboard._compat import StrEnum

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

_LEVEL: Final = 1


class ContentEncoding(StrEnum):
    """A `Content-Encoding` the server takes a request body in."""

    ZSTD = "zstd"


class TooLargeError(ValueError):
    """A body that decompresses to more than it may."""


def compress(body: bytes) -> bytes:
    return zstd.compress(body, level=_LEVEL)


def decompress(body: bytes, limit: int) -> bytes:
    """
    `body` decompressed, if that is at most `limit` bytes: never more than one byte past it is ever made.

    Raises `TooLargeError` past it, and `ValueError` for a body that isn't one whole zstd frame.
    """
    decompressor = zstd.ZstdDecompressor()
    try:
        plain = decompressor.decompress(body, max_length=limit + 1)
    except zstd.ZstdError as error:
        raise ValueError(str(error)) from error
    if len(plain) > limit:
        msg = f"the body decompresses to more than {limit} bytes"
        raise TooLargeError(msg)
    if not decompressor.eof:
        msg = "the body is not a whole zstd frame"
        raise ValueError(msg)
    return plain
