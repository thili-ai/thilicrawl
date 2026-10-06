"""Concurrency limits (lessons 3 and 7).

Unbounded concurrency is not "as fast as possible", it is "as broken as possible": two thousand
simultaneous requests overload the server you are checking, exhaust your own connection pool and file
descriptors, and turn a clean run into a wall of timeouts. Concurrency is a *dial*, and the right
setting is found by measuring (see ``bench/``).

``HostLimiter`` turns the dial three ways, and they stack:

* a **global** limit: the most requests in flight overall (protects *us*);
* a **per-host** limit: the most to any one host (protects *them* — one slow third-party site cannot
  hog every slot, and we do not hammer one server with the whole budget);
* an optional **rate** per host: a minimum gap between request *starts*, for hosts that care about
  requests per second and not just how many are in flight at once.

The three are separate because they protect different things, and a limit on the wrong one is no
protection at all.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class HostLimiter:
    def __init__(
        self,
        concurrency: int,
        per_host: int,
        rate_per_host: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if concurrency < 1 or per_host < 1:
            raise ValueError("limits must be at least 1")
        self._global = asyncio.Semaphore(concurrency)
        self._per_host = per_host
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._interval = 1.0 / rate_per_host if rate_per_host else 0.0
        self._rate_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_start: dict[str, float] = {}
        self._clock = clock
        self._sleep = sleep
        self.in_flight = 0
        self.peak_in_flight = 0

    def _host_semaphore(self, host: str) -> asyncio.Semaphore:
        if host not in self._hosts:
            self._hosts[host] = asyncio.Semaphore(self._per_host)
        return self._hosts[host]

    async def _wait_for_rate(self, host: str) -> None:
        if not self._interval:
            return
        async with self._rate_locks[host]:  # one waiter at a time, so start times stay in order
            now = self._clock()
            start = max(now, self._next_start.get(host, now))
            self._next_start[host] = start + self._interval
            if start > now:
                await self._sleep(start - now)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        """Hold a place in line for one request to ``host``. Released on exit, however it exits."""
        await self._wait_for_rate(host)  # wait for our turn *before* taking a slot, so we hold none idle
        async with self._host_semaphore(host), self._global:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                yield
            finally:
                self.in_flight -= 1
