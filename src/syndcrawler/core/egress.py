from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from syndcrawler.core.url import canonicalize_url

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


class UnsafeTargetError(ValueError):
    """Raised when a URL would violate crawler egress policy."""


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Central policy for network destinations used by every fetch engine.

    Production defaults allow only public HTTP(S) destinations. Private-network
    crawling is an explicit opt-in for development and intranet deployments.
    """

    allow_private_networks: bool = False

    async def validate(self, value: str, *, resolver: Resolver | None = None) -> str:
        try:
            url = canonicalize_url(value)
        except (ValueError, UnicodeError) as exc:
            raise UnsafeTargetError(str(exc)) from exc

        parsed = urlsplit(url)
        host = parsed.hostname
        assert host is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await self._validate_host(host, port, resolver=resolver)
        return url

    async def validate_proxy(self, value: str, *, resolver: Resolver | None = None) -> str:
        """Validate an HTTP(S) proxy endpoint while allowing proxy credentials."""

        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"}:
                raise UnsafeTargetError("proxy URL must use http or https")
            if not parsed.hostname:
                raise UnsafeTargetError("proxy URL must include a hostname")
            if parsed.port is None:
                raise UnsafeTargetError("proxy URL must include an explicit port")
        except ValueError as exc:
            raise UnsafeTargetError(str(exc)) from exc

        await self._validate_host(parsed.hostname, parsed.port, resolver=resolver)
        return value

    async def _validate_host(
        self,
        host: str,
        port: int,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        lowered = host.lower().rstrip(".")
        if (
            not self.allow_private_networks
            and (lowered == "localhost" or lowered.endswith((".localhost", ".local")))
        ):
            raise UnsafeTargetError(f"local hostname is blocked: {host}")

        literal = _parse_ip(host)
        if literal is not None:
            self._validate_ip(literal)
            return

        resolved = await (resolver or resolve_host)(host, port)
        if not resolved:
            raise UnsafeTargetError(f"hostname did not resolve: {host}")

        for address in resolved:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeTargetError(f"resolver returned invalid IP: {address}") from exc
            self._validate_ip(ip)

    def _validate_ip(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
        if self.allow_private_networks:
            return
        if not ip.is_global:
            raise UnsafeTargetError(f"non-public network destination is blocked: {ip}")


async def resolve_host(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(info[4][0] for info in infos))


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
