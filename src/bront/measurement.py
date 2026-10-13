"""Shared bounded traffic scheduling and percentile calculation."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import count as counter
from time import monotonic


def percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile; an empty population has no observed latency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


@dataclass
class LoadResult:
    dispatched: int
    elapsed_seconds: float
    sample_limit_reached: bool


async def drive_load[T](
    request: Callable[[int, float], Awaitable[T]],
    *,
    concurrency: int,
    count: int | None,
    duration_seconds: float | None = None,
    origin: float | None = None,
    record: Callable[[T], None] | None = None,
    clock: Callable[[], float] = monotonic,
    max_samples: int | None = None,
) -> LoadResult:
    if (count is None) == (duration_seconds is None):
        raise ValueError("Specify exactly one request count or duration")
    started = clock() if origin is None else origin
    deadline = started + duration_seconds if duration_seconds is not None else None
    indices = iter(range(count)) if count is not None else counter()
    dispatched = 0
    limited = False

    async def worker() -> None:
        nonlocal dispatched, limited
        # No await between selecting the next index and owning the request.
        for index in indices:
            if deadline is not None and clock() >= deadline:
                return
            if max_samples is not None and index >= max_samples:
                limited = True
                return
            dispatched += 1
            result = await request(index, started)
            if record is not None:
                record(result)
            await asyncio.sleep(0)  # Fair scheduling even with immediately failed requests.

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, count) if count is not None else concurrency):
            group.create_task(worker())
    return LoadResult(dispatched, clock() - started, limited)
