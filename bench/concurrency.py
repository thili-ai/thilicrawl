"""Lessons 1, 3 and 5: how much does concurrency buy, and what does *unbounded* concurrency cost?

    python bench/concurrency.py

The same 300 URLs, checked five ways, against a test site in a separate process (so the server never
competes with the client for the interpreter lock). Then the same flood against a server that can only
handle 40 requests at once, to show why "gather everything" is not a plan.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from thilicrawl import CrawlConfig, Outcome, check_urls
from thilicrawl.testsite import SiteConfig, SiteProcess

N = 300
LATENCY_MS = 50


def urls_for(base: str, n: int) -> list[str]:
    return [f"{base}/p/{1 + i % 400}" for i in range(n)]


def sequential(urls: list[str]) -> int:
    with httpx.Client() as client:
        return sum(client.head(u).status_code == 200 for u in urls)


def threads(urls: list[str], workers: int = 16) -> int:
    with (
        httpx.Client(limits=httpx.Limits(max_connections=workers)) as client,
        ThreadPoolExecutor(workers) as pool,
    ):
        return sum(pool.map(lambda u: client.head(u).status_code == 200, urls))


async def unbounded(urls: list[str]) -> tuple[int, int]:
    """``gather`` every request at once. Fast until the server pushes back."""
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=None)) as client:
        replies = await asyncio.gather(*(client.head(u) for u in urls), return_exceptions=True)
    ok = sum(isinstance(r, httpx.Response) and r.status_code == 200 for r in replies)
    return ok, len(urls) - ok


async def semaphore(urls: list[str], limit: int = 20) -> tuple[int, int]:
    gate = asyncio.Semaphore(limit)
    async with httpx.AsyncClient() as client:

        async def one(url: str) -> bool:
            async with gate:
                return (await client.head(url)).status_code == 200

        replies = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    ok = sum(r is True for r in replies)
    return ok, len(urls) - ok


def timed(label: str, fn, *args) -> None:  # type: ignore[no-untyped-def]
    start = time.perf_counter()
    out = fn(*args)
    took = time.perf_counter() - start
    print(f"  {label:<34} {took:6.2f}s   {N / took:7.1f} URLs/s   {out}")


def main() -> None:
    print(f"Checking {N} URLs, each taking ~{LATENCY_MS} ms on the server\n")
    with SiteProcess(SiteConfig(pages=400, latency_ms=LATENCY_MS, faults=False)) as site:
        urls = urls_for(site.main_url, N)
        timed("one at a time (httpx.Client)", sequential, urls)
        timed("16 threads", threads, urls)
        timed("asyncio.gather, unbounded", lambda u: asyncio.run(unbounded(u)), urls)
        timed("asyncio + Semaphore(20)", lambda u: asyncio.run(semaphore(u)), urls)
        timed(
            "thilicrawl.check_urls (20)",
            lambda u: asyncio.run(check_urls(u, CrawlConfig(concurrency=20, per_host=20))).count()[
                Outcome.OK
            ],
            urls,
        )

    print("\nNow against a server that can only handle 40 requests at once (it answers 503 beyond that)\n")
    with SiteProcess(SiteConfig(pages=400, latency_ms=100, capacity=40, faults=False)) as site:
        urls = urls_for(site.main_url, 600)
        for label, runner in (
            ("unbounded gather of 600", lambda: asyncio.run(unbounded(urls))),
            ("Semaphore(20)", lambda: asyncio.run(semaphore(urls))),
        ):
            start = time.perf_counter()
            ok, failed = runner()
            print(f"  {label:<26} {time.perf_counter() - start:5.2f}s   ok={ok:<4} failed={failed}")
        stats = site.stats()
        print(
            f"\n  server saw {stats['requests']} requests, turned away {stats['overloaded']}, "
            f"peak {stats['max_inflight']} in flight"
        )


if __name__ == "__main__":
    main()
