from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    """Configuration shared by embedded and local CLI crawls."""

    max_pages: int = 100
    max_concurrency: int = 10
    max_retries: int = 2
    same_domain: bool = True
    respect_robots_txt: bool = True
    allow_private_networks: bool = False
    output: Path | None = None

    def __post_init__(self) -> None:
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
