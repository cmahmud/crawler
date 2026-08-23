import pytest

from syndcrawler.config import CrawlConfig
from syndcrawler.core.routes import ProxyMode


def test_required_proxy_mode_needs_proxy() -> None:
    with pytest.raises(ValueError):
        CrawlConfig(proxy_mode=ProxyMode.REQUIRED)


def test_proxy_config_accepts_pool() -> None:
    config = CrawlConfig(
        proxy_urls=("http://proxy.example:8000",),
        proxy_mode=ProxyMode.REQUIRED,
    )
    assert config.proxy_urls == ("http://proxy.example:8000",)
