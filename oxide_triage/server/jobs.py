"""Background jobs for the admin panel: warm the cache, load the demo fixture, run the
self-check, add a material, fill gaps. One job runs at a time; its log lines are captured so
the panel can show them, and its outcome is kept until the next job starts.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from oxide_triage.server.store import now_iso

KINDS = ("warm", "fixtures", "selfcheck", "add_material", "fill_gaps")


class JobState(BaseModel):
    id: str
    kind: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "queued"  # queued | running | done | failed
    started_at: str = Field(default_factory=now_iso)
    finished_at: str | None = None
    log: list[str] = Field(default_factory=list)
    outcome: Any = None
    error: str | None = None


class _Capture(logging.Handler):
    def __init__(self, job: JobState, lock: threading.Lock):
        super().__init__(level=logging.INFO)
        self.job = job
        self.lock = lock

    def emit(self, record: logging.LogRecord) -> None:
        line = f"{time.strftime('%H:%M:%S')} {record.getMessage()}"
        with self.lock:
            self.job.log.append(line)
            if len(self.job.log) > 2000:
                del self.job.log[: len(self.job.log) - 2000]


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: JobState | None = None
        self._thread: threading.Thread | None = None
        self._n = 0

    @property
    def current(self) -> JobState | None:
        return self._current

    def snapshot(self) -> JobState | None:
        with self._lock:
            return self._current.model_copy(deep=True) if self._current else None

    def start(self, kind: str, fn: Callable[[JobState], Any], args: dict[str, Any] | None = None) -> JobState:
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}; one of {KINDS}")
        with self._lock:
            if self._current is not None and self._current.status in {"queued", "running"}:
                raise RuntimeError(f"a {self._current.kind} job is already running")
            self._n += 1
            job = JobState(id=f"job-{self._n}", kind=kind, args=args or {})
            self._current = job
        handler = _Capture(job, self._lock)
        logger = logging.getLogger("oxide_triage")

        def run() -> None:
            previous_level = logger.level
            logger.addHandler(handler)
            if logger.level == logging.NOTSET or logger.level > logging.INFO:
                logger.setLevel(logging.INFO)
            with self._lock:
                job.status = "running"
            try:
                outcome = fn(job)
                with self._lock:
                    job.outcome = outcome
                    job.status = "done"
            except Exception as exc:  # the panel shows the failure; the server keeps running
                with self._lock:
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.log.append(traceback.format_exc().rstrip().splitlines()[-1])
                    job.status = "failed"
            finally:
                with self._lock:
                    job.finished_at = now_iso()
                logger.removeHandler(handler)
                logger.setLevel(previous_level)

        self._thread = threading.Thread(target=run, name=f"job-{kind}", daemon=True)
        self._thread.start()
        return job
