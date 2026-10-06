"""Lessons 4, 5, 7 and 10: what happens when the network misbehaves?

    python bench/failure_modes.py

Four experiments, each against a server that misbehaves in one way:

1. a host that accepts the connection and never answers (the deadline);
2. a page that says "429, come back in a few seconds" (``Retry-After``);
3. a flaky page: fails twice, then works (retry with backoff);
4. Ctrl-C in the middle of a crawl (partial results, exit code 130).
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import time

from thilicrawl import CrawlConfig, RetryPolicy, check_urls
from thilicrawl.testsite import SiteConfig, SiteProcess


def show(label: str, report, started: float) -> None:  # type: ignore[no-untyped-def]
    for r in report.results.values():
        detail = r.error or f"{r.status}"
        print(
            f"  {label:<26} {r.outcome.value:<8} attempts={r.attempts} "
            f"after {time.perf_counter() - started:5.1f}s  {detail}"
        )


async def hung_host(site: SiteProcess) -> None:
    url = site.ext_url + "/x/hang/0"
    for label, config in (
        (
            "timeout 30s, deadline 3s",
            CrawlConfig(timeout=30.0, deadline=3.0, retry=RetryPolicy(max_attempts=1)),
        ),
        ("timeout 1s, 1 attempt", CrawlConfig(timeout=1.0, deadline=20.0, retry=RetryPolicy(max_attempts=1))),
        (
            "timeout 1s, 3 attempts",
            CrawlConfig(timeout=1.0, deadline=20.0, retry=RetryPolicy(max_attempts=3, base_delay=0.2)),
        ),
        (
            "timeout 1s, deadline 2.5s",
            CrawlConfig(timeout=1.0, deadline=2.5, retry=RetryPolicy(max_attempts=3, base_delay=0.2)),
        ),
    ):
        started = time.perf_counter()
        show(label, await check_urls([url], config), started)


async def rate_limited(site: SiteProcess) -> None:
    config = CrawlConfig(concurrency=1, per_host=1, retry=RetryPolicy(max_attempts=4, base_delay=0.1))
    started = time.perf_counter()
    report = await check_urls([site.main_url + f"/limited/{i}" for i in range(2)], config)
    show("limited pages", report, started)


async def flaky(site: SiteProcess) -> None:
    started = time.perf_counter()
    report = await check_urls(
        [site.main_url + "/flaky/0"], CrawlConfig(retry=RetryPolicy(max_attempts=4, base_delay=0.2))
    )
    show("flaky page (2 failures)", report, started)


def ctrl_c(site: SiteProcess) -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "thilicrawl",
            "check",
            site.main_url + "/",
            "--concurrency",
            "4",
            "--external",
            "--no-robots",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.5)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=30)
    print(f"  exit code {proc.returncode}; stderr: {err.strip()!r}")
    print("  " + out.splitlines()[0] if out else "  (no output)")


def main() -> None:
    config = SiteConfig(pages=300, latency_ms=20, limited=2, limit_requests=1, limit_window_s=2.0)
    with SiteProcess(config) as site:
        print("1. A host that never answers")
        asyncio.run(hung_host(site))
        print("\n2. A rate-limited page (1 request per 2 s, then 429 + Retry-After)")
        asyncio.run(rate_limited(site))
        print("\n3. A flaky page")
        asyncio.run(flaky(site))
        print("\n4. Ctrl-C part-way through a crawl")
        ctrl_c(site)


if __name__ == "__main__":
    main()
