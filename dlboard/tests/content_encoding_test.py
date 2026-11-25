"""`dlboard._content_encoding`: a body comes back as it was sent, unless it decompresses past the limit (#799)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlboard._content_encoding import TooLargeError, compress, decompress

# Incompressible, or not at all.
_BODIES = st.binary(max_size=64) | st.binary(min_size=1, max_size=4).map(lambda b: b * 500)


@given(_BODIES, st.integers(min_value=0, max_value=3000))
def test_a_body_decompresses_to_itself_unless_it_is_longer_than_the_limit(body: bytes, limit: int) -> None:
    sent = compress(body)
    if len(body) > limit:
        with pytest.raises(TooLargeError):
            decompress(sent, limit)
    else:
        assert decompress(sent, limit) == body


def test_a_body_cut_short_or_not_zstd_at_all_is_refused() -> None:
    sent = compress(b"x" * 1000)
    for broken in (sent[: len(sent) // 2], b"plainly not " + sent):
        with pytest.raises(ValueError, match=r"\w") as raised:
            decompress(broken, 10_000)
        assert not isinstance(raised.value, TooLargeError)
