from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syndcrawler.core.routes import ProxyMode

_BROWSER_TYPES = {"chromium", "chrome", "firefox", "webkit"}


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    """Configuration shared by embedded and local CLI crawls."""

    max_pages: int = 100
    max_concurrency: int = 10
    max_retries: int = 2
    same_domain: bool = True
    respect_robots_txt: bool = True
    allow_private_networks: bool = False
    proxy_urls: tuple[str, ...] = ()
    proxy_mode: ProxyMode = ProxyMode.AUTO
    browser_enabled: bool = False
    browser_type: str = "chromium"
    browser_executable_path: Path | None = None
    browser_max_concurrency: int = 2
    browser_navigation_timeout_seconds: float = 45.0
    browser_settle_seconds: float = 0.25
    browser_chromium_sandbox: bool = True
    output: Path | None = None

    def __post_init__(self) -> None:
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.proxy_mode is ProxyMode.REQUIRED and not self.proxy_urls:
            raise ValueError("proxy_mode='required' needs at least one proxy URL")
        if self.browser_type not in _BROWSER_TYPES:
            raise ValueError(f"unsupported browser_type: {self.browser_type}")
        if self.browser_max_concurrency <= 0:
            raise ValueError("browser_max_concurrency must be positive")
        if self.browser_navigation_timeout_seconds <= 0:
            raise ValueError("browser_navigation_timeout_seconds must be positive")
        if self.browser_settle_seconds < 0:
            raise ValueError("browser_settle_seconds cannot be negative")
