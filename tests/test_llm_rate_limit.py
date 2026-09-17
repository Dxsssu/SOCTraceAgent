import pytest
from src.agent.rate_limit import LLMRateLimiter


def test_token_budget_waits_until_completed_reservation_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('src.agent.rate_limit.time.monotonic', lambda: clock[0])
    limiter = LLMRateLimiter(rpm=10, tpm=100, window_seconds=60)
    monkeypatch.setattr(limiter._condition, 'wait', lambda timeout: clock.__setitem__(0, clock[0] + timeout))
    first = limiter.acquire(80)
    limiter.finish(first, 80)
    limiter.acquire(30)
    assert clock[0] == 60


def test_rpm_limit_is_independent_of_token_limit(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('src.agent.rate_limit.time.monotonic', lambda: clock[0])
    limiter = LLMRateLimiter(rpm=1, tpm=1000, window_seconds=60)
    monkeypatch.setattr(limiter._condition, 'wait', lambda timeout: clock.__setitem__(0, clock[0] + timeout))
    first = limiter.acquire(1)
    limiter.finish(first, 1)
    limiter.acquire(1)
    assert clock[0] == 60


def test_actual_usage_releases_excess_reservation():
    limiter = LLMRateLimiter(rpm=10, tpm=100)
    first = limiter.acquire(90)
    limiter.finish(first, 10)
    assert limiter.acquire(80).tokens == 80


def test_oversized_request_fails_without_waiting():
    with pytest.raises(ValueError, match='TPM'):
        LLMRateLimiter(rpm=10, tpm=100).acquire(101)
