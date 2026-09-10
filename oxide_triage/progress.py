"""Progress events emitted by long-running operations (a query's on-demand fill, a cache warm).

A ``ProgressFn`` receives ``Progress`` values; the CLI logs them, the web app streams them. The
callback is optional everywhere and must never influence a result: it is observation only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Progress:
    stage: str  # parse | rank | fill | refute | done | warm
    message: str
    done: int | None = None
    total: int | None = None


ProgressFn = Callable[[Progress], None]


def emit(
    fn: ProgressFn | None, stage: str, message: str, done: int | None = None, total: int | None = None
) -> None:
    if fn is not None:
        fn(Progress(stage=stage, message=message, done=done, total=total))
