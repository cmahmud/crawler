from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from syndcrawler.core.policy import AdaptivePolicy, FetchAction, FetchEngine, NetworkRoute


class ProxyMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    action: FetchAction
    proxy_url: str | None


@dataclass(slots=True)
class _PendingDecision:
    context_key: str
    decision: RouteDecision
    started_at: float


class ProxyPool:
    """Small sticky/round-robin proxy pool used behind the route policy."""

    def __init__(self, urls: tuple[str, ...]) -> None:
        if not urls:
            raise ValueError("proxy pool requires at least one URL")
        self.urls = urls
        self._cursor = 0
        self._sticky: dict[str, str] = {}

    def next(self, session_id: str | None = None) -> str:
        if session_id is not None and session_id in self._sticky:
            return self._sticky[session_id]

        url = self.urls[self._cursor % len(self.urls)]
        self._cursor += 1
        if session_id is not None:
            self._sticky[session_id] = url
        return url


class RouteBroker:
    """Select direct/proxy routes per engine and learn from contextual outcomes."""

    def __init__(
        self,
        *,
        proxy_pool: ProxyPool | None = None,
        mode: ProxyMode = ProxyMode.AUTO,
        policy: AdaptivePolicy | None = None,
    ) -> None:
        if mode is ProxyMode.REQUIRED and proxy_pool is None:
            raise ValueError("proxy mode 'required' needs a proxy pool")
        self.proxy_pool = proxy_pool
        self.mode = mode
        self.policy = policy or AdaptivePolicy()
        self._pending: dict[str, _PendingDecision] = {}

    def choose(
        self,
        context_key: str,
        request_key: str,
        *,
        engine: FetchEngine = FetchEngine.HTTP,
        session_id: str | None = None,
    ) -> RouteDecision:
        existing = self.pending_decision(request_key)
        if existing is not None:
            return existing

        actions = self._available_actions(engine)
        action = self.policy.choose(context_key, actions)
        proxy_url = None
        if action.route is NetworkRoute.PROXY:
            assert self.proxy_pool is not None
            proxy_url = self.proxy_pool.next(session_id)

        decision = RouteDecision(action=action, proxy_url=proxy_url)
        self._pending[request_key] = _PendingDecision(
            context_key=context_key,
            decision=decision,
            started_at=time.monotonic(),
        )
        return decision

    def observe(
        self,
        request_key: str,
        *,
        success: bool,
        quality: float,
        latency_ms: float | None = None,
        cost: float = 0.0,
    ) -> None:
        pending = self._pending.pop(request_key, None)
        if pending is None:
            return
        if latency_ms is None:
            latency_ms = (time.monotonic() - pending.started_at) * 1000
        self.policy.observe(
            pending.context_key,
            pending.decision.action,
            success=success,
            quality=quality,
            latency_ms=latency_ms,
            cost=cost,
        )

    def pending_decision(self, request_key: str) -> RouteDecision | None:
        pending = self._pending.get(request_key)
        return pending.decision if pending is not None else None

    def selected_action(self, request_key: str) -> FetchAction | None:
        decision = self.pending_decision(request_key)
        return decision.action if decision is not None else None

    def _available_actions(self, engine: FetchEngine) -> list[FetchAction]:
        direct = FetchAction(engine, NetworkRoute.DIRECT)
        proxy = FetchAction(engine, NetworkRoute.PROXY)
        if self.mode is ProxyMode.DIRECT or self.proxy_pool is None:
            return [direct]
        if self.mode is ProxyMode.REQUIRED:
            return [proxy]
        return [direct, proxy]
