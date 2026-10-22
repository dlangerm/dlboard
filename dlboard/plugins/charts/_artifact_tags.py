"""An artifact's tags as a panel dataframe carries them, read back and formatted for a chart's captions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from dlboard.models import TagDisplay, TagValue, format_tag_value

if TYPE_CHECKING:
    from collections.abc import Mapping

_TAGS_ADAPTER: TypeAdapter[dict[str, TagValue]] = TypeAdapter(dict[str, TagValue])


def parse_tags(raw_tags: object) -> dict[str, TagValue]:
    """
    An artifact `<key>__tags` cell as typed tags.

    `build_artifacts_dataframe` puts each artifact's tags in as plain JSON (a `Quantity` is a
    `{"value": ..., "unit": ...}` dict by then), and a `float` (NaN) where pivoting left a gap for a
    (run, step) with no tags logged -- the sole non-dict case, which has none.
    """
    return _TAGS_ADAPTER.validate_python(raw_tags) if isinstance(raw_tags, dict) else {}


def format_tags(
    raw_tags: object,
    display: TagDisplay = TagDisplay(),  # noqa: B008
    by_key: Mapping[str, TagDisplay] | None = None,
) -> dict[str, str]:
    """
    Format an artifact `<key>__tags` cell into each tag's name and its value as shown.

    The cell is read with `parse_tags`.
    Every tag is shown as `display`, except one named in `by_key`, which that overrides.
    """
    by_key = by_key or {}
    return {
        k: format_tag_value(v, display.overridden_by(by_key[k]) if k in by_key else display)
        for k, v in parse_tags(raw_tags).items()
    }


def format_tags_caption(
    raw_tags: object,
    display: TagDisplay = TagDisplay(),  # noqa: B008
    by_key: Mapping[str, TagDisplay] | None = None,
) -> str:
    """Format an artifact `<key>__tags` cell into a caption: `format_tags` on one line."""
    return ", ".join(f"{k}: {v}" for k, v in format_tags(raw_tags, display, by_key).items())
