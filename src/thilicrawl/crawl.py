"""The crawler (lessons 3, 6, 7 and 8): a queue of URLs, a pool of workers, and a clean way to stop.

        start URL
            |
            v
    +----------------+      get       +-----------+   check()   +--------------+
    |  frontier      | ------------>  |  worker   | ----------> | LinkChecker  |  (client.py)
    |  asyncio.Queue | <------------  |  x N      |             +--------------+
    +----------------+  new links     +-----------+
            ^                              |
            +--- visited set --- dedupe ---+ ...and each result goes into the report

Four ideas, one per lesson:

**The frontier is unbounded on purpose** (lesson 6). A bounded queue sounds like backpressure, but here
the *workers themselves* feed the queue: if it fills while every worker is blocked trying to add to it,
nobody is left to take anything out and the crawl deadlocks. The bound that matters is elsewhere: the
visited set stops duplicates, ``max_pages`` stops runaway sites, and the **worker count** is the concurrency
limit. (Backpressure is real for a *separate* consumer, which is what ``sink`` is for.)

**Politeness is per host** (lesson 7). ``HostLimiter`` caps requests overall *and* to each host.

**Structured concurrency** (lesson 8). Workers live inside an ``asyncio.TaskGroup``: if one hits a bug,
the group cancels the others and raises an ``ExceptionGroup`` — nothing is left running unseen. Expected
failures (a 404, a timeout) never reach the group; they are *results*.

**Keep the event loop free** (lesson 9). Parsing a huge page is CPU work; see ``Crawler._extract``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

import httpx

from .client import LinkChecker, Verdict
from .limits import HostLimiter
from .models import CrawlConfig, CrawlReport, LinkResult, Outcome
from .parse import extract_links, origin

Sink = Callable[[LinkResult], Awaitable[None]]


@dataclass(slots=True)
class _Item:
    url: str
    depth: int


class Crawler:
    """Check a site's links (``follow=True``: start from one page and crawl) or just a list of URLs."""

    def __init__(
        self,
        seeds: Sequence[str],
        config: CrawlConfig | None = None,
        *,
        follow: bool = True,
        list_method: str = "HEAD",
        transport: httpx.AsyncBaseTransport | None = None,
        sink: Sink | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not seeds:
            raise ValueError("nothing to check: give at least one URL")
        self._cfg = config or CrawlConfig()
        self._seeds = list(seeds)
        self._follow = follow
        self._list_method = list_method
        self._origin = origin(seeds[0])
        self._transport = transport
        self._sink = sink
        self._sleep = sleep
        self._rng = rng
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._seen: set[str] = set()
        self._referrers: dict[str, list[str]] = {}
        self._pool: ProcessPoolExecutor | None = None
        self._checker: LinkChecker | None = None
        self.limiter: HostLimiter | None = None
        self.report = CrawlReport(start_url=seeds[0])  # filled in as results arrive: readable mid-crawl

    # -- discovery ---------------------------------------------------------------------------------

    def _enqueue(self, url: str, depth: int, referrer: str | None) -> None:
        if referrer is not None:
            known = self._referrers.setdefault(url, [])
            if referrer not in known and len(known) < 5:
                known.append(referrer)
        if url in self._seen:
            return
        if self._cfg.max_pages is not None and len(self._seen) >= self._cfg.max_pages:
            return
        if self._follow and not self._cfg.check_external and origin(url) != self._origin:
            return
        self._seen.add(url)
        self._queue.put_nowait(_Item(url, depth))

    # -- the work ------------------------------------------------------------------------------------

    async def _extract(self, body: str, base_url: str) -> list[str]:
        """Find the links in a page *without stalling every other task while we do it*.

        Parsing is CPU work, and an ``async`` program has one thread: while this runs, nothing else does.
        For an ordinary page that is microseconds. For a 7 MB page it is over a second — during which no
        socket is serviced, no timeout can fire, and (measured in ``bench/``) healthy links are reported
        *slow* because the clock kept running while we were not looking. So big pages leave the event loop:

        * ``inline``  — run it right here: fine for small pages, a stall for big ones;
        * ``thread``  — ``asyncio.to_thread``: the loop stays mostly responsive (the interpreter lock is
          shared, so it is not undisturbed) and the parsing is no *faster*;
        * ``process`` — a worker process: the loop is left alone, and several big pages can parse at once,
          at the cost of copying the page across;
        * ``auto``    — inline up to ``inline_parse_limit`` bytes, a process above it (falling back to a
          thread if a process cannot be started).
        """
        mode = self._cfg.parse_mode
        if mode == "auto":
            mode = "inline" if len(body) <= self._cfg.inline_parse_limit else "process"
        if mode == "inline":
            return extract_links(body, base_url)
        if mode == "process":
            try:
                if self._pool is None:
                    self._pool = ProcessPoolExecutor(max_workers=2)
                return await asyncio.get_running_loop().run_in_executor(
                    self._pool, extract_links, body, base_url
                )
            except (BrokenProcessPool, OSError):  # e.g. a sandbox that will not start processes
                self._pool = None
        return await asyncio.to_thread(extract_links, body, base_url)

    async def _process(self, item: _Item) -> None:
        assert self._checker is not None
        url, depth, cfg = item.url, item.depth, self._cfg
        internal = self._follow and origin(url) == self._origin
        referrers = tuple(self._referrers.get(url, ()))

        if internal and cfg.respect_robots:
            verdict, why = await self._checker.check_robots(url)
            if verdict is Verdict.DISALLOWED:
                self._record(
                    LinkResult(url, Outcome.SKIPPED, error="disallowed by robots.txt", referrers=referrers)
                )
                return
            if verdict is Verdict.UNAVAILABLE:
                self._record(
                    LinkResult(
                        url,
                        Outcome.ERROR,
                        referrers=referrers,
                        error=f"robots.txt could not be read ({why}); not fetching",
                    )
                )
                return

        wants_links = internal and (cfg.max_depth is None or depth < cfg.max_depth)
        checked = await self._checker.check(
            url,
            method="GET" if internal else self._list_method,
            want_body=wants_links,
            referrers=referrers,
        )
        result = checked.result
        if checked.body is not None and result.outcome is Outcome.OK:
            for link in await self._extract(checked.body, result.final_url or url):
                self._enqueue(link, depth + 1, url)
        if internal and result.outcome is Outcome.REDIRECT and result.final_url:
            self._enqueue(result.final_url, depth, url)  # a redirect inside the site: crawl where it leads
        self._record(result)

    def _record(self, result: LinkResult) -> None:
        self.report.results[result.url] = result
        if self._sink is not None:
            self._pending_sink.append(result)

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
                while self._pending_sink:  # awaiting a slow sink here is what makes backpressure work
                    assert self._sink is not None
                    await self._sink(self._pending_sink.pop())
            finally:
                self._queue.task_done()

    # -- running ---------------------------------------------------------------------------------------

    async def run(self) -> CrawlReport:
        cfg = self._cfg
        self._pending_sink: list[LinkResult] = []
        started = time.perf_counter()
        limits = httpx.Limits(max_connections=cfg.concurrency + 10, max_keepalive_connections=cfg.concurrency)
        self.limiter = HostLimiter(cfg.concurrency, cfg.per_host, cfg.rate_per_host)
        try:
            async with httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(cfg.timeout),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                self._checker = LinkChecker(cfg, client, self.limiter, sleep=self._sleep, rng=self._rng)
                for seed in self._seeds:
                    self._enqueue(seed, 0, None)
                async with asyncio.TaskGroup() as group:
                    workers = [group.create_task(self._worker()) for _ in range(cfg.concurrency)]
                    await self._queue.join()  # every URL found has been checked
                    for worker in workers:
                        worker.cancel()  # ...so the (endless) workers can go home
        finally:
            self.report.elapsed = time.perf_counter() - started
            if self._checker is not None:
                self.report.requests = self._checker.requests
            for url, found_on in self._referrers.items():
                if url in self.report.results:
                    self.report.results[url] = dataclasses.replace(
                        self.report.results[url], referrers=tuple(found_on)
                    )
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
        return self.report


async def check_links(start_url: str, config: CrawlConfig | None = None, **options: object) -> CrawlReport:
    """Crawl a site from ``start_url`` and check every link on it. The library's front door."""
    return await Crawler([start_url], config, follow=True, **options).run()  # type: ignore[arg-type]


async def check_urls(
    urls: Sequence[str], config: CrawlConfig | None = None, **options: object
) -> CrawlReport:
    """Check exactly these URLs (no crawling), concurrently and politely."""
    return await Crawler(urls, config, follow=False, **options).run()  # type: ignore[arg-type]
