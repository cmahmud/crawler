from __future__ import annotations

import os

from syndcrawler.config import CrawlConfig
from syndcrawler.runtime.server import ServerRuntime


async def server_runtime_from_env() -> ServerRuntime:
    """Construct a shared server runtime from deployment environment variables."""

    config = CrawlConfig(
        max_pages=env_int("SYNCRAWLER_MAX_PAGES", 100, minimum=1),
        max_concurrency=env_int("SYNCRAWLER_MAX_CONCURRENCY", 10, minimum=1),
        max_retries=env_int("SYNCRAWLER_MAX_RETRIES", 2, minimum=0),
        respect_robots_txt=env_bool("SYNCRAWLER_RESPECT_ROBOTS", True),
        allow_private_networks=env_bool("SYNCRAWLER_ALLOW_PRIVATE_NETWORKS", False),
        sitemap_discovery_enabled=env_bool("SYNCRAWLER_SITEMAP_DISCOVERY", True),
        browser_enabled=env_bool("SYNCRAWLER_BROWSER_ENABLED", False),
    )
    return await ServerRuntime.from_urls(
        redis_url=required_env("SYNCRAWLER_REDIS_URL"),
        postgres_dsn=required_env("SYNCRAWLER_POSTGRES_DSN"),
        config=config,
        redis_key_prefix=os.environ.get("SYNCRAWLER_REDIS_KEY_PREFIX", "syndcrawler"),
        postgres_min_pool_size=env_int("SYNCRAWLER_POSTGRES_POOL_MIN", 1, minimum=0),
        postgres_max_pool_size=env_int("SYNCRAWLER_POSTGRES_POOL_MAX", 10, minimum=1),
    )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def env_int(name: str, default: int, *, minimum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return parsed


def env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return parsed
