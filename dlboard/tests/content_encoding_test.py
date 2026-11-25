"""`dlboard._content_encoding`: a body comes back as it was sent, unless it decompresses past the limit (#799)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dlboard._content_encoding import ContentEncoding, TooLargeError, decode, encodable, encode

# Incompressible, or not at all.
_BODIES = st.binary(max_size=64) | st.binary(min_size=1, max_size=4).map(lambda b: b * 500)


@given(_BODIES, st.sampled_from(encodable()), st.integers(min_value=0, max_value=3000))
def test_a_body_decodes_to_itself_unless_it_is_longer_than_the_limit(
    body: bytes, encoding: ContentEncoding, limit: int
) -> None:
    sent = encode(body, encoding)
    if len(body) > limit:
        with pytest.raises(TooLargeError):
            decode(sent, encoding, limit)
    else:
        assert decode(sent, encoding, limit) == body


@pytest.mark.parametrize("encoding", encodable())
def test_a_body_cut_short_or_not_the_encoding_at_all_is_refused(encoding: ContentEncoding) -> None:
    sent = encode(b"x" * 1000, encoding)
    for broken in (sent[: len(sent) // 2], b"plainly not " + sent):
        with pytest.raises(ValueError, match=r"\w") as raised:
            decode(broken, encoding, 10_000)
        assert not isinstance(raised.value, TooLargeError)
