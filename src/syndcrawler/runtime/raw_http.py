from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import unquote, urljoin, urlsplit

from syndcrawler.config import CrawlConfig
from syndcrawler.core.change import HttpValidators
from syndcrawler.core.egress import EgressPolicy
from syndcrawler.core.policy import FetchEngine
from syndcrawler.core.routes import ProxyPool, RouteBroker
from syndcrawler.core.url import hostname

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class RawFetchError(RuntimeError):
    """Raised when a bounded raw fetch cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class RawHttpResponse:
    requested_url: str
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    redirect_chain: tuple[str, ...]
    engine: str
    route: str

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304


class SafeRawHttpFetcher:
    """Bounded Impit fetcher that validates every redirect hop before connecting."""

    def __init__(
        self,
        config: CrawlConfig | None = None,
        *,
        egress: EgressPolicy | None = None,
        max_redirects: int = 10,
    ) -> None:
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        self.config = config or CrawlConfig()
        self.egress = egress or EgressPolicy(
            allow_private_networks=self.config.allow_private_networks,
        )
        self.max_redirects = max_redirects

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float | None = None,
        validators: HttpValidators | None = None,
    ) -> RawHttpResponse:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        timeout_value = (
            self.config.browser_navigation_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if timeout_value <= 0:
            raise ValueError("timeout_seconds must be positive")

        from crawlee.http_clients import ImpitHttpClient

        requested_url = await self.egress.validate(url)
        safe_proxies = tuple(
            [await self.egress.validate_proxy(value) for value in self.config.proxy_urls]
        )
        proxy_pool = ProxyPool(safe_proxies) if safe_proxies else None
        broker = RouteBroker(proxy_pool=proxy_pool, mode=self.config.proxy_mode)
        request_key = f"raw:{uuid.uuid4().hex}"
        decision = broker.choose(
            f"{hostname(requested_url)}:document",
            request_key,
            engine=FetchEngine.HTTP,
        )
        proxy_info = _proxy_info(decision.proxy_url)
        client = ImpitHttpClient(follow_redirects=False)
        current_url = requested_url
        redirects: list[str] = []
        conditional_headers = validators.request_headers() if validators else {}

        try:
            for hop in range(self.max_redirects + 1):
                async with client.stream(
                    current_url,
                    headers=conditional_headers or None,
                    proxy_info=proxy_info,
                    timeout=timedelta(seconds=timeout_value),
                ) as response:
                    status_code = response.status_code
                    headers = {
                        str(key).lower(): str(value)
                        for key, value in response.headers.items()
                    }

                    if status_code in _REDIRECT_STATUS_CODES:
                        location = headers.get("location")
                        if not location:
                            body = await _read_bounded(response, max_bytes=max_bytes)
                            broker.observe(request_key, success=True, quality=0.5)
                            return RawHttpResponse(
                                requested_url=requested_url,
                                url=current_url,
                                status_code=status_code,
                                headers=headers,
                                body=body,
                                redirect_chain=tuple(redirects),
                                engine=decision.action.engine.value,
                                route=decision.action.route.value,
                            )
                        if hop >= self.max_redirects:
                            raise RawFetchError(
                                f"redirect limit exceeded for {requested_url}"
                            )
                        next_url = await self.egress.validate(
                            urljoin(current_url, location)
                        )
                        if not _same_origin(current_url, next_url):
                            conditional_headers = {}
                        redirects.append(next_url)
                        current_url = next_url
                        continue

                    content_length = _content_length(headers.get("content-length"))
                    if content_length is not None and content_length > max_bytes:
                        raise RawFetchError(
                            f"response exceeds {max_bytes} bytes by Content-Length"
                        )
                    body = await _read_bounded(response, max_bytes=max_bytes)
                    quality = 1.0 if 200 <= status_code < 400 else 0.25
                    broker.observe(request_key, success=True, quality=quality)
                    return RawHttpResponse(
                        requested_url=requested_url,
                        url=current_url,
                        status_code=status_code,
                        headers=headers,
                        body=body,
                        redirect_chain=tuple(redirects),
                        engine=decision.action.engine.value,
                        route=decision.action.route.value,
                    )
        except Exception:
            broker.observe(request_key, success=False, quality=0.0)
            raise
        finally:
            await client.cleanup()

        raise RawFetchError(f"fetch did not produce a response: {requested_url}")


async def _read_bounded(response, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.read_stream():
        total += len(chunk)
        if total > max_bytes:
            raise RawFetchError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _same_origin(first: str, second: str) -> bool:
    left = urlsplit(first)
    right = urlsplit(second)
    return (
        left.scheme.lower(),
        (left.hostname or "").lower(),
        left.port or _default_port(left.scheme),
    ) == (
        right.scheme.lower(),
        (right.hostname or "").lower(),
        right.port or _default_port(right.scheme),
    )


def _default_port(scheme: str) -> int | None:
    if scheme.lower() == "http":
        return 80
    if scheme.lower() == "https":
        return 443
    return None


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _proxy_info(url: str | None):
    if url is None:
        return None
    from crawlee.proxy_configuration import ProxyInfo

    parsed = urlsplit(url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    return ProxyInfo(
        url=url,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )
