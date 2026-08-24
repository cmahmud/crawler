from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from syndcrawler.core.url import canonicalize_url


@dataclass(frozen=True, slots=True)
class StructuredEvidence:
    canonical_url: str | None
    json_ld: tuple[Any, ...]
    open_graph: dict[str, tuple[str, ...]]
    malformed_json_ld_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "json_ld": list(self.json_ld),
            "open_graph": {
                key: list(values) for key, values in self.open_graph.items()
            },
            "malformed_json_ld_count": self.malformed_json_ld_count,
        }


@dataclass(frozen=True, slots=True)
class ParsedHtml:
    title: str | None
    links: tuple[str, ...]
    structured: StructuredEvidence


def parse_html(html: bytes | str, source_url: str) -> ParsedHtml:
    """Parse navigation and structured evidence from one HTML document."""

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

    canonical_url = _extract_canonical_url(parser, source_url)
    json_ld, malformed_json_ld_count = _extract_json_ld(parser)
    open_graph = _extract_open_graph(parser)

    return ParsedHtml(
        title=title,
        links=tuple(links),
        structured=StructuredEvidence(
            canonical_url=canonical_url,
            json_ld=tuple(json_ld),
            open_graph=open_graph,
            malformed_json_ld_count=malformed_json_ld_count,
        ),
    )


def _extract_canonical_url(parser: LexborHTMLParser, source_url: str) -> str | None:
    for node in parser.css("link[href][rel]"):
        rel = node.attributes.get("rel", "")
        if "canonical" not in {part.lower() for part in rel.split()}:
            continue
        href = node.attributes.get("href")
        if not href:
            continue
        try:
            return canonicalize_url(urljoin(source_url, href))
        except (ValueError, UnicodeError):
            return None
    return None


def _extract_json_ld(parser: LexborHTMLParser) -> tuple[list[Any], int]:
    payloads: list[Any] = []
    malformed = 0
    for node in parser.css("script[type]"):
        content_type = node.attributes.get("type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/ld+json":
            continue
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            malformed += 1
    return payloads, malformed


def _extract_open_graph(parser: LexborHTMLParser) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for node in parser.css("meta[property][content]"):
        key = node.attributes.get("property", "").strip().lower()
        if not key.startswith("og:"):
            continue
        content = node.attributes.get("content", "").strip()
        if not content:
            continue
        values.setdefault(key, []).append(content)
    return {key: tuple(items) for key, items in values.items()}
