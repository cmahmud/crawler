from syndcrawler.core.policy import AdaptivePolicy, FetchAction, FetchEngine, NetworkRoute

HTTP = FetchAction(FetchEngine.HTTP, NetworkRoute.DIRECT)
BROWSER = FetchAction(FetchEngine.BROWSER, NetworkRoute.DIRECT)


def test_http_direct_is_cold_start_default() -> None:
    policy = AdaptivePolicy(exploration_interval=100)
    assert policy.choose("example.com:product", [BROWSER, HTTP]) == HTTP


def test_policy_learns_browser_when_http_quality_fails() -> None:
    policy = AdaptivePolicy(alpha=0.5, exploration_interval=100)
    key = "example.com:product"
    for _ in range(6):
        policy.observe(key, HTTP, success=False, quality=0.0, latency_ms=150)
        policy.observe(key, BROWSER, success=True, quality=1.0, latency_ms=800)
    assert policy.choose(key, [HTTP, BROWSER]) == BROWSER


def test_policy_explores_least_sampled_action() -> None:
    policy = AdaptivePolicy(exploration_interval=2)
    key = "example.com:unknown"
    policy.observe(key, HTTP, success=True, quality=1.0, latency_ms=100)
    assert policy.choose(key, [HTTP, BROWSER]) == HTTP
    assert policy.choose(key, [HTTP, BROWSER]) == BROWSER
