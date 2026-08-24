from __future__ import annotations

from dataclasses import dataclass

from syndcrawler.core.change import ChangeKind


@dataclass(frozen=True, slots=True)
class RecrawlPolicy:
    """Deterministic adaptive interval policy for repeat fetches."""

    min_interval_seconds: float = 15 * 60
    base_interval_seconds: float = 24 * 60 * 60
    max_interval_seconds: float = 30 * 24 * 60 * 60
    stable_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be positive")
        if self.base_interval_seconds < self.min_interval_seconds:
            raise ValueError("base_interval_seconds must be >= min_interval_seconds")
        if self.max_interval_seconds < self.base_interval_seconds:
            raise ValueError("max_interval_seconds must be >= base_interval_seconds")
        if self.stable_multiplier <= 1:
            raise ValueError("stable_multiplier must be > 1")

    def next_interval(
        self,
        change_kind: ChangeKind,
        previous_interval_seconds: float | None,
    ) -> float:
        previous = (
            self.base_interval_seconds
            if previous_interval_seconds is None or previous_interval_seconds <= 0
            else previous_interval_seconds
        )

        if change_kind is ChangeKind.NEW:
            return self.base_interval_seconds
        if change_kind in {ChangeKind.NOT_MODIFIED, ChangeKind.UNCHANGED}:
            return min(self.max_interval_seconds, previous * self.stable_multiplier)
        if change_kind is ChangeKind.REPRESENTATION_CHANGED:
            return max(
                self.min_interval_seconds,
                min(self.base_interval_seconds, previous),
            )
        if change_kind is ChangeKind.SEMANTIC_CHANGED:
            return self.min_interval_seconds
        raise ValueError(f"unsupported change kind: {change_kind}")

    def next_fetch_at(
        self,
        now: float,
        change_kind: ChangeKind,
        previous_interval_seconds: float | None,
    ) -> tuple[float, float]:
        interval = self.next_interval(change_kind, previous_interval_seconds)
        return interval, now + interval
