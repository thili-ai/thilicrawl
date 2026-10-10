from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest

from thilicrawl.limits import HostLimiter

from .helpers import FakeTime


async def hold(limiter: HostLimiter, host: str, seconds: float = 0.01) -> None:
    async with limiter.slot(host):
        await asyncio.sleep(seconds)


async def test_the_global_limit_is_never_exceeded() -> None:
    limiter = HostLimiter(concurrency=3, per_host=100)
    await asyncio.gather(*(hold(limiter, f"host{i % 7}") for i in range(40)))
    assert limiter.peak_in_flight == 3
    assert limiter.in_flight == 0


async def test_the_per_host_limit_is_never_exceeded_even_when_the_global_one_would_allow_more() -> None:
    limiter = HostLimiter(concurrency=50, per_host=2)
    await asyncio.gather(*(hold(limiter, "only.test") for _ in range(30)))
    assert limiter.peak_in_flight == 2


async def test_one_busy_host_does_not_block_another() -> None:
    limiter = HostLimiter(concurrency=10, per_host=1)
    slow = asyncio.create_task(hold(limiter, "slow.test", seconds=0.3))
    await asyncio.sleep(0.02)  # the slow host now holds its only slot
    started = asyncio.get_running_loop().time()
    await hold(limiter, "other.test", seconds=0.01)
    assert asyncio.get_running_loop().time() - started < 0.15  # did not wait for slow.test
    await slow


async def test_a_task_waiting_for_its_host_does_not_hold_a_global_slot() -> None:
    """Lock order: the narrow resource (the host's slot) first, the broad one (a global slot) second.

    Taken the other way round, tasks queued behind a busy host sit on global slots doing nothing, and a
    perfectly idle host cannot get one.
    """
    limiter = HostLimiter(concurrency=2, per_host=1)
    busy = [asyncio.create_task(hold(limiter, "busy.test", seconds=0.3)) for _ in range(3)]
    await asyncio.sleep(0.02)  # one is running; the other two are waiting for busy.test's single slot
    started = asyncio.get_running_loop().time()
    await hold(limiter, "idle.test", seconds=0.01)
    assert asyncio.get_running_loop().time() - started < 0.15  # did not queue behind busy.test's backlog
    await asyncio.gather(*busy)


async def test_a_slot_is_released_when_the_work_inside_it_fails() -> None:
    limiter = HostLimiter(concurrency=1, per_host=1)
    with pytest.raises(RuntimeError):
        async with limiter.slot("h"):
            raise RuntimeError("boom")
    assert limiter.in_flight == 0
    await asyncio.wait_for(hold(limiter, "h"), timeout=1)  # would hang forever if the slot had leaked


async def test_a_slot_is_released_when_the_task_is_cancelled() -> None:
    limiter = HostLimiter(concurrency=1, per_host=1)
    task = asyncio.create_task(hold(limiter, "h", seconds=5))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(hold(limiter, "h"), timeout=1)


async def test_rate_limit_spaces_request_starts_apart() -> None:
    fake = FakeTime()
    limiter = HostLimiter(concurrency=10, per_host=10, rate_per_host=4.0, clock=fake.clock, sleep=fake.sleep)
    starts: list[float] = []

    async def go() -> None:
        async with limiter.slot("h"):
            starts.append(fake.clock())

    await asyncio.gather(*(go() for _ in range(5)))
    gaps = [round(b - a, 6) for a, b in pairwise(starts)]
    assert gaps == [0.25, 0.25, 0.25, 0.25]  # 4 requests per second: one every quarter second


async def test_rate_limit_applies_per_host() -> None:
    fake = FakeTime()
    limiter = HostLimiter(concurrency=10, per_host=10, rate_per_host=1.0, clock=fake.clock, sleep=fake.sleep)

    async def go(host: str) -> None:
        async with limiter.slot(host):
            pass

    await asyncio.gather(go("a"), go("b"), go("c"))  # three hosts, one request each: no one waits
    assert fake.sleeps == []


async def test_no_rate_means_no_waiting() -> None:
    fake = FakeTime()
    limiter = HostLimiter(concurrency=10, per_host=10, clock=fake.clock, sleep=fake.sleep)
    await asyncio.gather(*(hold(limiter, "h", 0) for _ in range(10)))
    assert fake.sleeps == []


@pytest.mark.parametrize(("concurrency", "per_host"), [(0, 1), (1, 0), (-1, 5)])
def test_nonsense_limits_are_refused(concurrency: int, per_host: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        HostLimiter(concurrency, per_host)
