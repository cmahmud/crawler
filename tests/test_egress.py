import pytest

from syndcrawler.core.egress import EgressPolicy, UnsafeTargetError


async def static_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def private_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("10.0.0.8",)


@pytest.mark.asyncio
async def test_allows_public_destination() -> None:
    policy = EgressPolicy()
    assert (
        await policy.validate("https://Example.com/a#x", resolver=static_resolver)
        == "https://example.com/a"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://localhost/",
    ],
)
async def test_blocks_non_public_destinations(url: str) -> None:
    with pytest.raises(UnsafeTargetError):
        await EgressPolicy().validate(url, resolver=static_resolver)


@pytest.mark.asyncio
async def test_blocks_hostname_resolving_to_private_address() -> None:
    with pytest.raises(UnsafeTargetError):
        await EgressPolicy().validate("https://example.test/", resolver=private_resolver)


@pytest.mark.asyncio
async def test_private_network_opt_in() -> None:
    policy = EgressPolicy(allow_private_networks=True)
    assert await policy.validate("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/"


@pytest.mark.asyncio
async def test_proxy_credentials_allowed_but_destination_is_validated() -> None:
    policy = EgressPolicy()
    value = "http://user:pass@proxy.example:8000"
    assert await policy.validate_proxy(value, resolver=static_resolver) == value


@pytest.mark.asyncio
async def test_private_proxy_is_blocked_by_default() -> None:
    with pytest.raises(UnsafeTargetError):
        await EgressPolicy().validate_proxy(
            "http://user:pass@proxy.example:8000",
            resolver=private_resolver,
        )
