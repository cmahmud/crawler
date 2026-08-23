from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from syndcrawler.core.url import canonicalize_url


@dataclass(frozen=True, slots=True)
class ParsedHtml:
    title: str | None
    links: tuple[str, ...]


def parse_html(html: bytes | str, source_url: str) -> ParsedHtml:
    """Parse title and canonical HTTP(S) links from one HTML document."""

    parser = LexborHTMLParser(html)
    title_node = parser.css_first("title")
    title = title_node.text(strip=True) if title_node is not None else None

    seen: set[str] = set()
    links: list[str] = []
    for node in parser.css("a[href]"):
        href = node.attributes.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            candidate = canonicalize_url(urljoin(source_url, href))
        except (ValueError, UnicodeError):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        links.append(candidate)

    return ParsedHtml(title=title, links=tuple(links))
