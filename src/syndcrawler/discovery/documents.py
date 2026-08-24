from __future__ import annotations

import gzip
import io
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree

from syndcrawler.core.url import canonicalize_url

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_XML_FORBIDDEN_MARKERS = (b"<!doctype", b"<!entity")


class DiscoveryParseError(ValueError):
    """Raised when an untrusted discovery document is invalid or unsafe to parse."""


@dataclass(frozen=True, slots=True)
class DiscoveryDocument:
    kind: str
    urls: tuple[str, ...]
    nested_documents: tuple[str, ...] = ()


def parse_sitemap(
    payload: bytes | str,
    source_url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> DiscoveryDocument:
    """Parse XML, gzip-compressed XML, or plain-text sitemap documents."""

    raw = _bounded_bytes(payload, max_bytes=max_bytes)
    if raw.startswith(b"\x1f\x8b"):
        raw = _bounded_gzip(raw, max_bytes=max_bytes)

    stripped = raw.lstrip()
    if not stripped.startswith(b"<"):
        urls = _parse_text_url_list(raw, source_url)
        return DiscoveryDocument(kind="sitemap-text", urls=urls)

    root = _parse_xml(raw)
    root_name = _local_name(root.tag)
    if root_name == "urlset":
        return DiscoveryDocument(
            kind="sitemap-urlset",
            urls=_xml_loc_values(root, "url", source_url),
        )
    if root_name == "sitemapindex":
        return DiscoveryDocument(
            kind="sitemap-index",
            urls=(),
            nested_documents=_xml_loc_values(root, "sitemap", source_url),
        )
    raise DiscoveryParseError(f"unsupported sitemap root element: {root_name}")


def parse_feed(
    payload: bytes | str,
    source_url: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> DiscoveryDocument:
    """Parse RSS/RDF or Atom feeds into crawlable entry URLs."""

    raw = _bounded_bytes(payload, max_bytes=max_bytes)
    root = _parse_xml(raw)
    root_name = _local_name(root.tag).lower()

    if root_name in {"rss", "rdf"}:
        urls: list[str] = []
        for item in root.iter():
            if _local_name(item.tag).lower() != "item":
                continue
            for child in item:
                if _local_name(child.tag).lower() != "link":
                    continue
                candidate = _safe_join(source_url, child.text)
                if candidate is not None:
                    urls.append(candidate)
                    break
        return DiscoveryDocument(kind="feed-rss", urls=_dedupe(urls))

    if root_name == "feed":
        urls = []
        for entry in root.iter():
            if _local_name(entry.tag).lower() != "entry":
                continue
            links: list[tuple[int, str]] = []
            for child in entry:
                if _local_name(child.tag).lower() != "link":
                    continue
                href = child.attrib.get("href")
                candidate = _safe_join(source_url, href)
                if candidate is None:
                    continue
                rel = child.attrib.get("rel", "alternate").lower()
                priority = 0 if rel in {"", "alternate"} else 1
                links.append((priority, candidate))
            if links:
                links.sort(key=lambda item: item[0])
                urls.append(links[0][1])
        return DiscoveryDocument(kind="feed-atom", urls=_dedupe(urls))

    raise DiscoveryParseError(f"unsupported feed root element: {root_name}")


def parse_robots_sitemaps(text: bytes | str, source_url: str) -> tuple[str, ...]:
    """Extract Sitemap directives from robots.txt without interpreting allow/disallow rules."""

    value = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    urls: list[str] = []
    for raw_line in value.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, raw_value = line.split(":", 1)
        if field.strip().lower() != "sitemap":
            continue
        candidate = _safe_join(source_url, raw_value.strip())
        if candidate is not None:
            urls.append(candidate)
    return _dedupe(urls)


def _bounded_bytes(payload: bytes | str, *, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > max_bytes:
        raise DiscoveryParseError(f"discovery document exceeds {max_bytes} bytes")
    return raw


def _bounded_gzip(payload: bytes, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            while True:
                chunk = stream.read(min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DiscoveryParseError(
                        f"decompressed sitemap exceeds {max_bytes} bytes"
                    )
                chunks.append(chunk)
    except OSError as exc:
        raise DiscoveryParseError("invalid gzip sitemap") from exc
    return b"".join(chunks)


def _parse_xml(raw: bytes) -> ElementTree.Element:
    lowered = raw[: min(len(raw), 256 * 1024)].lower()
    if any(marker in lowered for marker in _XML_FORBIDDEN_MARKERS):
        raise DiscoveryParseError("DTD/entity declarations are not allowed")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DiscoveryParseError(f"invalid XML discovery document: {exc}") from exc


def _xml_loc_values(
    root: ElementTree.Element,
    parent_name: str,
    source_url: str,
) -> tuple[str, ...]:
    urls: list[str] = []
    for parent in root.iter():
        if _local_name(parent.tag).lower() != parent_name:
            continue
        for child in parent:
            if _local_name(child.tag).lower() != "loc":
                continue
            candidate = _safe_join(source_url, child.text)
            if candidate is not None:
                urls.append(candidate)
            break
    return _dedupe(urls)


def _parse_text_url_list(raw: bytes, source_url: str) -> tuple[str, ...]:
    value = raw.decode("utf-8", errors="replace")
    urls = []
    for line in value.splitlines():
        candidate = _safe_join(source_url, line.strip())
        if candidate is not None:
            urls.append(candidate)
    return _dedupe(urls)


def _safe_join(source_url: str, value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return canonicalize_url(urljoin(source_url, value.strip()))
    except (ValueError, UnicodeError):
        return None


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
