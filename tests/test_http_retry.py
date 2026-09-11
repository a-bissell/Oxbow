"""Retry policy of the shared HTTP client: bounded backoff, and no multi-hour sleeps on a
Retry-After header (OpenAlex sends ~22 h when the daily budget is spent)."""

from __future__ import annotations

import httpx
import pytest

from oxide_triage.sources.base import MAX_RETRY_AFTER_S, Http, SourceError


def _client(handler, sleeps):
    http = Http(max_retries=2, recorder=None)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    http._sleep = lambda attempt, delay=None: sleeps.append(
        delay if delay is not None else min(2**attempt, 8)
    )
    return http


def test_long_retry_after_gives_up_immediately_with_reason():
    calls, sleeps = [], []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(429, headers={"Retry-After": "78420"}, text='{"error":"Insufficient budget"}')

    http = _client(handler, sleeps)
    with pytest.raises(SourceError) as exc:
        http.get_json("https://api.example.org/works", {"q": "x"})
    assert len(calls) == 1 and sleeps == []
    assert "78420" in str(exc.value) and "Insufficient budget" in str(exc.value)


def test_short_retry_after_is_honoured_then_succeeds():
    n, sleeps = [0], []

    def handler(request):
        n[0] += 1
        if n[0] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    http = _client(handler, sleeps)
    assert http.get_json("https://api.example.org/v") == {"ok": True}
    assert sleeps == [2.0] and 2.0 <= MAX_RETRY_AFTER_S


def test_429_without_header_backs_off_then_gives_up():
    sleeps = []
    http = _client(lambda r: httpx.Response(429), sleeps)
    with pytest.raises(SourceError, match="Giving up"):
        http.get_json("https://api.example.org/v")
    assert sleeps == [1, 2, 4]


# ---- request-rate cap ---------------------------------------------------------------------


def test_rate_limiter_spaces_requests_across_threads():
    import time
    from concurrent.futures import ThreadPoolExecutor

    from oxide_triage.sources.base import RateLimiter

    lim = RateLimiter(max_rps=50)  # 20 ms apart
    started = time.monotonic()
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: lim.wait(), range(10)))
    elapsed = time.monotonic() - started
    assert elapsed >= 9 * 0.02 * 0.9  # ten slots, nine intervals, some scheduling slack


def test_http_applies_limiter_to_every_attempt():
    calls, sleeps = [], []
    n = [0]

    def handler(request):
        n[0] += 1
        calls.append(request.url)
        return httpx.Response(429) if n[0] < 3 else httpx.Response(200, json={"ok": 1})

    http = _client(handler, sleeps)
    waits = []
    http.limiter = type("L", (), {"wait": lambda self: waits.append(1)})()
    assert http.get_json("https://api.example.org/v") == {"ok": 1}
    assert len(calls) == 3 and len(waits) == 3  # the two retries were rate-capped too


def test_rate_limiter_rejects_nonpositive_rate():
    from oxide_triage.sources.base import RateLimiter

    with pytest.raises(ValueError):
        RateLimiter(0)


# ---- circuit breaker ---------------------------------------------------------------------------


def _breaker_client(handler, sleeps, threshold=3, cooldown=60.0):
    from oxide_triage.sources.base import CircuitBreaker

    http = _client(handler, sleeps)
    http.breaker = CircuitBreaker(threshold=threshold, cooldown_s=cooldown, name="oqmd")
    return http


def test_breaker_opens_after_consecutive_server_failures_and_fails_fast(monkeypatch):
    """One formula's retries (3 attempts at max_retries=2) trip a threshold of 3; the next
    formula never reaches the transport and the error names the pause."""
    calls, sleeps = [], []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(502)

    http = _breaker_client(handler, sleeps, threshold=3)
    with pytest.raises(SourceError, match="Giving up"):
        http.get_json("https://oqmd.org/a")
    assert len(calls) == 3 and http.breaker.opened == 1
    with pytest.raises(SourceError, match="oqmd paused for .*after 3 consecutive server failures"):
        http.get_json("https://oqmd.org/b")
    assert len(calls) == 3  # not even one attempt while paused

    # After the cooldown one request is let through; a success closes the breaker.
    now = [1000.0]
    monkeypatch.setattr("oxide_triage.sources.base.time.monotonic", lambda: now[0])
    http.breaker._open_until = now[0] + 60.0
    now[0] += 61.0
    ok = {"ok": True}
    http._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=ok)))
    assert http.get_json("https://oqmd.org/c") == ok
    assert http.breaker._failures == 0 and http.breaker._open_until == 0.0


def test_breaker_ignores_rate_limit_responses_and_resets_on_any_answer():
    n, sleeps = [0], []

    def handler(request):
        n[0] += 1
        if n[0] <= 2:
            return httpx.Response(429)  # the rate cap's business, not the breaker's
        if n[0] == 3:
            return httpx.Response(200, json={"n": 3})
        return httpx.Response(404)  # a served "no record" still means the server is up

    http = _breaker_client(handler, sleeps, threshold=2)
    assert http.get_json("https://oqmd.org/a") == {"n": 3}
    assert http.breaker.opened == 0 and http.breaker._failures == 0
    assert http.get_json("https://oqmd.org/b") is None
    assert http.breaker.opened == 0
