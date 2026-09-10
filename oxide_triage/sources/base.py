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

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from oxide_triage.cache import Cache

log = logging.getLogger(__name__)

FetchStatus = str  # cached | fetched | stale_cached | missing_offline | fetch_failed


class SourceError(Exception):
    """A remote source could not be reached or returned an unusable response."""


VOLATILE_PARAMS = {"mailto"}  # excluded from recording keys; never affect the response shape
# Longest a single retry will wait on a Retry-After header. OpenAlex answers an exhausted daily
# budget with 429 + Retry-After of ~22 hours; sleeping that long would silently hang every worker.
# Beyond this cap the request is given up immediately with the header value in the error.
MAX_RETRY_AFTER_S = 60.0
RECORD_ENV = "OXIDE_TRIAGE_RECORD_DIR"


def request_key(url: str, params: dict[str, Any] | None) -> str:
    clean = {k: v for k, v in (params or {}).items() if k not in VOLATILE_PARAMS}
    blob = json.dumps({"url": url, "params": clean}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


class Recorder:
    """Writes every successful response to ``<dir>/<host>/<key>.json``. Headers (which carry
    the API key) are never written. Set ``OXIDE_TRIAGE_RECORD_DIR`` to record a live cache warm;
    the files become replayable fixtures for tests (see ``ReplayHttp``)."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    @classmethod
    def from_env(cls) -> Recorder | None:
        d = os.environ.get(RECORD_ENV)
        return cls(d) if d else None

    def save(self, url: str, params: dict[str, Any] | None, response: Any) -> Path:
        host = httpx.URL(url).host or "unknown"
        path = self.dir / host / f"{request_key(url, params)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in (params or {}).items() if k not in VOLATILE_PARAMS}
        path.write_text(
            json.dumps(
                {"url": url, "params": clean, "response": response}, indent=1, sort_keys=True, default=str
            ),
            encoding="utf-8",
        )
        return path


class ReplayHttp:
    """Serves recorded responses; anything unrecorded raises ``SourceError``. Drop-in for ``Http``."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.index: dict[str, Path] = {p.stem: p for p in self.dir.rglob("*.json")}

    def __len__(self) -> int:
        return len(self.index)

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        self.calls.append((url, params))
        path = self.index.get(request_key(url, params))
        if path is None:
            raise SourceError(f"no recording for {url} {params}")
        return json.loads(path.read_text(encoding="utf-8"))["response"]

    def close(self) -> None:
        return None


class Http:
    """Thin httpx wrapper: timeouts, retries with backoff, 429 handling, proxy-aware, optional
    recording of every response for replay in tests."""

    def __init__(
        self,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        user_agent: str = "",
        recorder: Recorder | None = None,
    ):
        headers = {"Accept": "application/json"}
        if user_agent:
            headers["User-Agent"] = user_agent
        self._client = httpx.Client(timeout=timeout_s, headers=headers, trust_env=True)
        self.max_retries = max_retries
        self.recorder = recorder if recorder is not None else Recorder.from_env()

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        data = self._get_json(url, params, headers)
        if self.recorder is not None:
            self.recorder.save(url, params, data)
        return data

    def _get_json(
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
                if delay is not None and delay > MAX_RETRY_AFTER_S:
                    # The server is telling us to come back much later (quota exhausted, not a
                    # burst). Retrying now is pointless and waiting would stall the whole warm.
                    raise SourceError(
                        f"HTTP {resp.status_code} from {url}: Retry-After {int(delay)}s exceeds "
                        f"{int(MAX_RETRY_AFTER_S)}s; giving up. {resp.text[:160]}"
                    )
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
