from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FetchEngine(StrEnum):
    HTTP = "http"
    BROWSER = "browser"


class NetworkRoute(StrEnum):
    DIRECT = "direct"
    PROXY = "proxy"


@dataclass(frozen=True, slots=True)
class FetchAction:
    engine: FetchEngine
    route: NetworkRoute = NetworkRoute.DIRECT

    @property
    def key(self) -> str:
        return f"{self.engine.value}:{self.route.value}"


@dataclass(slots=True)
class ActionStats:
    samples: int = 0
    success_ema: float = 0.5
    quality_ema: float = 0.5
    latency_ema_ms: float = 1000.0
    cost_ema: float = 0.0


class AdaptivePolicy:
    """Small contextual policy learner for fetch engine/network route selection.

    The first version deliberately uses transparent EMA scoring rather than an
    opaque model. It is easy to inspect, persist, benchmark and replace later.
    """

    def __init__(self, *, alpha: float = 0.2, exploration_interval: int = 20) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        if exploration_interval <= 0:
            raise ValueError("exploration_interval must be positive")
        self.alpha = alpha
        self.exploration_interval = exploration_interval
        self._stats: dict[tuple[str, str], ActionStats] = {}
        self._decisions: dict[str, int] = {}

    def choose(self, context_key: str, actions: list[FetchAction]) -> FetchAction:
        if not actions:
            raise ValueError("at least one fetch action is required")

        decision = self._decisions.get(context_key, 0) + 1
        self._decisions[context_key] = decision

        if decision % self.exploration_interval == 0:
            return min(actions, key=lambda action: self.stats(context_key, action).samples)

        return max(
            actions,
            key=lambda action: self._score(self.stats(context_key, action), action),
        )

    def observe(
        self,
        context_key: str,
        action: FetchAction,
        *,
        success: bool,
        quality: float,
        latency_ms: float,
        cost: float = 0.0,
    ) -> None:
        if not 0 <= quality <= 1:
            raise ValueError("quality must be between 0 and 1")
        if latency_ms < 0 or cost < 0:
            raise ValueError("latency and cost cannot be negative")

        stats = self.stats(context_key, action)
        stats.samples += 1
        stats.success_ema = self._ema(stats.success_ema, 1.0 if success else 0.0)
        stats.quality_ema = self._ema(stats.quality_ema, quality)
        stats.latency_ema_ms = self._ema(stats.latency_ema_ms, latency_ms)
        stats.cost_ema = self._ema(stats.cost_ema, cost)

    def stats(self, context_key: str, action: FetchAction) -> ActionStats:
        return self._stats.setdefault((context_key, action.key), ActionStats())

    def _ema(self, old: float, new: float) -> float:
        return self.alpha * new + (1 - self.alpha) * old

    @staticmethod
    def _score(stats: ActionStats, action: FetchAction) -> float:
        success_quality = stats.success_ema * stats.quality_ema
        latency_penalty = min(stats.latency_ema_ms / 20_000.0, 0.25)
        cost_penalty = min(stats.cost_ema, 0.25)
        browser_penalty = 0.04 if action.engine is FetchEngine.BROWSER else 0.0
        proxy_penalty = 0.02 if action.route is NetworkRoute.PROXY else 0.0
        cold_start_bonus = (
            0.015 if stats.samples == 0 and action.engine is FetchEngine.HTTP else 0.0
        )
        return (
            success_quality
            - latency_penalty
            - cost_penalty
            - browser_penalty
            - proxy_penalty
            + cold_start_bonus
        )
