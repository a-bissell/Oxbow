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
