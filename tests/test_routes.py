from syndcrawler.core.policy import AdaptivePolicy, FetchAction, FetchEngine, NetworkRoute
from syndcrawler.core.routes import ProxyMode, ProxyPool, RouteBroker


def test_direct_mode_never_returns_proxy() -> None:
    broker = RouteBroker(
        proxy_pool=ProxyPool(("http://proxy.example:8000",)),
        mode=ProxyMode.DIRECT,
    )
    decision = broker.choose("example.com:unknown", "request-1")
    assert decision.action.route is NetworkRoute.DIRECT
    assert decision.proxy_url is None


def test_required_mode_uses_sticky_proxy_session() -> None:
    broker = RouteBroker(
        proxy_pool=ProxyPool(
            (
                "http://proxy-a.example:8000",
                "http://proxy-b.example:8000",
            )
        ),
        mode=ProxyMode.REQUIRED,
    )
    first = broker.choose("example.com:unknown", "request-1", session_id="abc")
    second = broker.choose("example.com:unknown", "request-2", session_id="abc")
    assert first.proxy_url == second.proxy_url
    assert first.action.route is NetworkRoute.PROXY


def test_required_mode_preserves_requested_engine() -> None:
    broker = RouteBroker(
        proxy_pool=ProxyPool(("http://proxy.example:8000",)),
        mode=ProxyMode.REQUIRED,
    )
    decision = broker.choose(
        "example.com:product",
        "browser-request",
        engine=FetchEngine.BROWSER,
    )
    assert decision.action.engine is FetchEngine.BROWSER
    assert decision.action.route is NetworkRoute.PROXY


def test_pending_route_decision_is_idempotent() -> None:
    broker = RouteBroker(
        proxy_pool=ProxyPool(("http://proxy.example:8000",)),
        mode=ProxyMode.AUTO,
    )
    first = broker.choose("example.com:unknown", "same-request")
    second = broker.choose(
        "different-context",
        "same-request",
        engine=FetchEngine.BROWSER,
    )
    assert second is first


def test_direct_route_outcomes_are_learned_without_proxy_pool() -> None:
    policy = AdaptivePolicy(exploration_interval=100)
    broker = RouteBroker(mode=ProxyMode.AUTO, policy=policy)
    key = "example.com:unknown"
    decision = broker.choose(key, "direct-only")
    broker.observe("direct-only", success=True, quality=0.9, latency_ms=120)

    assert decision.action == FetchAction(FetchEngine.HTTP, NetworkRoute.DIRECT)
    assert policy.stats(key, decision.action).samples == 1


def test_auto_mode_learns_proxy_when_direct_fails() -> None:
    policy = AdaptivePolicy(alpha=0.5, exploration_interval=2)
    broker = RouteBroker(
        proxy_pool=ProxyPool(("http://proxy.example:8000",)),
        mode=ProxyMode.AUTO,
        policy=policy,
    )
    key = "example.com:product"

    direct = broker.choose(key, "direct")
    assert direct.action.route is NetworkRoute.DIRECT
    broker.observe("direct", success=False, quality=0, latency_ms=100)

    explored = broker.choose(key, "proxy")
    assert explored.action.route is NetworkRoute.PROXY
    broker.observe("proxy", success=True, quality=1, latency_ms=120)

    learned = broker.choose(key, "learned")
    assert learned.action.route is NetworkRoute.PROXY
