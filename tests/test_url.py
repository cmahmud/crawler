import pytest

from syndcrawler.core.url import canonicalize_url, same_hostname


def test_canonicalize_url_is_conservative() -> None:
    assert canonicalize_url("HTTPS://Example.COM:443/a?b=2&a=1#frag") == "https://example.com/a?b=2&a=1"
    assert canonicalize_url("/next", base="https://Example.com/a") == "https://example.com/next"


def test_canonicalize_rejects_unsupported_and_userinfo() -> None:
    with pytest.raises(ValueError):
        canonicalize_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        canonicalize_url("https://user:pass@example.com/")


def test_same_hostname_handles_case_and_default_ports() -> None:
    assert same_hostname("https://EXAMPLE.com", "http://example.com:8080/path")
