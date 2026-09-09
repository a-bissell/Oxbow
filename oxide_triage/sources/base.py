"""Shared plumbing for public-data clients: HTTP with retries, and cache-through access.

Every client follows the same contract:

    payload, retrieved_at, status = source.cached(key, fetch_fn)

``status`` is one of ``cached``, ``fetched``, ``stale_cached``, ``missing_offline``,
``fetch_failed``. A ``None`` payload with ``missing_offline`` / ``fetch_failed`` becomes
``DataStatus.UNKNOWN`` downstream: the pipeline never invents a value to fill the hole.

Retrieved text (titles, descriptions) is stored verbatim and treated as *data*. Nothing in
this package ever interprets it as an instruction.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from oxide_triage.cache import Cache

log = logging.getLogger(__name__)

FetchStatus = str  # cached | fetched | stale_cached | missing_offline | fetch_failed


class SourceError(Exception):
    """A remote source could not be reached or returned an unusable response."""


class Http:
    """Thin httpx wrapper: timeouts, retries with backoff, 429 handling, proxy-aware."""

    def __init__(self, timeout_s: float = 30.0, max_retries: int = 3, user_agent: str = ""):
        headers = {"Accept": "application/json"}
        if user_agent:
            headers["User-Agent"] = user_agent
        self._client = httpx.Client(timeout=timeout_s, headers=headers, trust_env=True)
        self.max_retries = max_retries

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:  # network-level failure
                last_exc = exc
                self._sleep(attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                last_exc = SourceError(f"HTTP {resp.status_code} from {url}")
                self._sleep(attempt, delay)
                continue
            if resp.status_code == 404:
                return None  # a documented "no record" is a valid answer
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise SourceError(f"Non-JSON response from {url}") from exc
        raise SourceError(f"Giving up on {url}: {last_exc}")

    @staticmethod
    def _sleep(attempt: int, delay: float | None = None) -> None:
        time.sleep(delay if delay is not None else min(2**attempt, 8))

    def close(self) -> None:
        self._client.close()


class CachedSource:
    name: str = "source"

    def __init__(self, cache: Cache, ttl_days: int = 90, offline: bool = False):
        self.cache = cache
        self.ttl_days = ttl_days
        self.offline = offline

    def cached(self, key: str, fetch: Callable[[], Any]) -> tuple[Any | None, str | None, FetchStatus]:
        hit = self.cache.get(self.name, key)
        if hit is not None:
            payload, ts = hit
            if self.offline or self.cache.is_fresh(ts, self.ttl_days):
                return payload, ts, "cached"
        if self.offline:
            self.cache.log(self.name, key, "missing_offline")
            return None, None, "missing_offline"
        try:
            payload = fetch()
        except SourceError as exc:
            log.warning("%s fetch failed for %s: %s", self.name, key, exc)
            self.cache.log(self.name, key, "fetch_failed", str(exc))
            if hit is not None:  # stale but better than nothing, and labelled as such
                return hit[0], hit[1], "stale_cached"
            return None, None, "fetch_failed"
        ts = self.cache.put(self.name, key, payload)
        self.cache.log(self.name, key, "fetched")
        return payload, ts, "fetched"
