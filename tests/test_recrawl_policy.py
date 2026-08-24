import pytest

from syndcrawler.core.change import ChangeKind
from syndcrawler.core.recrawl import RecrawlPolicy


def test_recrawl_policy_backs_off_stable_pages_and_caps_interval() -> None:
    policy = RecrawlPolicy(
        min_interval_seconds=10,
        base_interval_seconds=100,
        max_interval_seconds=800,
        stable_multiplier=2,
    )

    assert policy.next_interval(ChangeKind.NEW, None) == 100
    assert policy.next_interval(ChangeKind.UNCHANGED, 100) == 200
    assert policy.next_interval(ChangeKind.NOT_MODIFIED, 400) == 800
    assert policy.next_interval(ChangeKind.NOT_MODIFIED, 800) == 800


def test_recrawl_policy_reacts_quickly_to_semantic_change() -> None:
    policy = RecrawlPolicy(
        min_interval_seconds=10,
        base_interval_seconds=100,
        max_interval_seconds=800,
    )

    assert policy.next_interval(ChangeKind.SEMANTIC_CHANGED, 800) == 10
    assert policy.next_interval(ChangeKind.REPRESENTATION_CHANGED, 800) == 100
    assert policy.next_interval(ChangeKind.REPRESENTATION_CHANGED, 50) == 50


def test_recrawl_policy_validates_configuration() -> None:
    with pytest.raises(ValueError):
        RecrawlPolicy(min_interval_seconds=0)
    with pytest.raises(ValueError):
        RecrawlPolicy(min_interval_seconds=100, base_interval_seconds=10)
    with pytest.raises(ValueError):
        RecrawlPolicy(base_interval_seconds=100, max_interval_seconds=10)
    with pytest.raises(ValueError):
        RecrawlPolicy(stable_multiplier=1)
