"""Helpers shared by the research archives (news and filings)."""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser

SKIPPED_TAGS = {"script", "style", "head", "ix:header"}
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}


class MissingCoverage(LookupError):
    """The archive was not fetched for the whole requested window. Never an empty result."""


class FutureDataError(Exception):
    """A read asked for time after the trusted cutoff."""


def stored_time(value: datetime) -> str:
    """One stored format, so stored instants compare correctly as strings."""
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def truncate(text: str, limit: int) -> str:
    """`text` cut so that it plus an explicit marker fits in `limit` characters."""
    if len(text) <= limit:
        return text
    marker = f"\n[truncated at {limit} characters]"
    return text[: limit - len(marker)] + marker


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self.skipping = max(0, self.skipping - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Visible text of an HTML document, one paragraph per line, without inline XBRL headers."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
