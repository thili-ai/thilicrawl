"""Test helpers: a fake clock, and a checker wired to a pretend network.

Async code is testable when **time and the network are under your control**. Two tools do that here:

* ``httpx.MockTransport`` replaces the network with a function that returns whatever the test wants —
  a 503, a hang, a redirect loop — instantly and deterministically;
* ``FakeTime`` replaces ``asyncio.sleep`` and the clock, so a test of "wait 2 seconds, then retry" runs in
  microseconds and can assert *exactly* how long the code asked to wait.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from thilicrawl.client import LinkChecker
from thilicrawl.limits import HostLimiter
from thilicrawl.models import CrawlConfig
from thilicrawl.retry import RetryPolicy

Handler = Callable[[httpx.Request], httpx.Response] | Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # still yield, so other tasks get their turn


class CountingStream(httpx.AsyncByteStream):
    """A response body that remembers how much of it was actually read."""

    def __init__(self, total: int, chunk: int = 1024) -> None:
        self.total = total
        self.chunk = chunk
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while self.read < self.total:
            size = min(self.chunk, self.total - self.read)
            self.read += size
            yield b"x" * size


FAST_RETRY = RetryPolicy(max_attempts=4, base_delay=0.1, max_delay=1.0)


def fast_config(**overrides: object) -> CrawlConfig:
    base: dict[str, object] = {
        "retry": FAST_RETRY,
        "timeout": 1.0,
        "deadline": 10.0,
        "concurrency": 8,
        "per_host": 8,
    }
    base.update(overrides)
    return CrawlConfig(**base)  # type: ignore[arg-type]


@asynccontextmanager
async def checker(
    handler: Handler, config: CrawlConfig | None = None, fake: FakeTime | None = None
) -> AsyncIterator[LinkChecker]:
    config = config or fast_config()
    fake = fake or FakeTime()
    limiter = HostLimiter(
        config.concurrency, config.per_host, config.rate_per_host, clock=fake.clock, sleep=fake.sleep
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        follow_redirects=False,
    ) as client:
        yield LinkChecker(config, client, limiter, sleep=fake.sleep, rng=random.Random(1))


def html(*links: str) -> httpx.Response:
    body = "<html><body>" + "".join(f'<a href="{link}">x</a>' for link in links) + "</body></html>"
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})
