from __future__ import annotations

from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit


def canonicalize_url(value: str, *, base: str | None = None) -> str:
    """Apply conservative URL canonicalization without changing query semantics."""

    if base is not None:
        value = urljoin(base, value)

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '<missing>'}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("userinfo in crawl targets is not supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")

    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    bracketed = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = bracketed if port is None else f"{bracketed}:{port}"
    path = parsed.path or "/"

    normalized = SplitResult(scheme, netloc, path, parsed.query, "")
    return urlunsplit(normalized)


def same_hostname(left: str, right: str) -> bool:
    return _hostname(left) == _hostname(right)


def hostname(value: str) -> str:
    return _hostname(value)


def _hostname(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    return parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
