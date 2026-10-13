import asyncio

import pytest

from bront.measurement import drive_load, percentile


def test_percentiles_use_nearest_rank():
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([9, 1, 5], 50) == 5
    assert percentile([4], 99) == 4


async def test_cancelled_worker_does_not_refill_after_request_absorbs_cancellation():
    started = asyncio.Event()
    calls = []

    async def request(index, origin):
        calls.append(index)
        if index == 0:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return index  # Simulate a transport swallowing cancellation during cleanup.
        return index

    task = asyncio.create_task(drive_load(request, concurrency=1, count=3))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [0]
