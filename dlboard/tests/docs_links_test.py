"""
The docs index lists every docs page, and every relative link in the docs reaches a file and heading that exist.

`docs/README.md` is the one list of pages, so a page it leaves out is one only a search finds; a link to a page
or heading that was renamed reads as fine until someone follows it.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
_DOCS = _ROOT / "docs"
_INDEX = _DOCS / "README.md"
_PAGES = sorted(str(page.relative_to(_ROOT)) for page in [_ROOT / "README.md", *_DOCS.rglob("*.md")])
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _links(page: Path) -> list[str]:
    """The relative links of `page`, outside code blocks."""
    text = _FENCE.sub("", page.read_text())
    return [link for link in _LINK.findall(text) if not re.match(r"[a-z]+:", link)]


def _anchors(page: Path) -> set[str]:
    """The ids a renderer gives `page`'s headings: lowercase, punctuation dropped, spaces as `-`."""
    headings: list[str] = re.findall(r"^#+ (.+)$", _FENCE.sub("", page.read_text()), re.MULTILINE)
    return {re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-") for heading in headings}


@pytest.mark.parametrize("page", [p for p in _PAGES if p.startswith("docs/") and p != "docs/README.md"])
def test_the_index_links_every_docs_page(page: str) -> None:
    linked = {(_DOCS / link.partition("#")[0]).resolve() for link in _links(_INDEX)}
    assert (_ROOT / page).resolve() in linked, f"add {page} to docs/README.md"


@pytest.mark.parametrize("page", _PAGES)
def test_every_relative_link_resolves(page: str) -> None:
    source = _ROOT / page
    for link in _links(source):
        path, _, anchor = link.partition("#")
        target = (source.parent / path).resolve() if path else source
        assert target.exists(), f"{link} in {page} is not a file"
        if anchor and target.suffix == ".md":
            assert anchor in _anchors(target), f"{link} in {page} is not a heading"
