"""Lesson 9: CPU-bound work on the event loop.

    python bench/parse_modes.py

Parsing one enormous page (150,000 links, ~7 MB) takes about the same time however you run it. What
changes is what *else* can happen meanwhile. Part 1 measures how long a heartbeat task is kept waiting;
part 2 shows the consequence: healthy links reported "slow" because the clock kept running while the
event loop was busy.

Run from a file with the ``__main__`` guard: process pools re-import the main module in the child.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ProcessPoolExecutor

from thilicrawl import CrawlConfig, check_links
from thilicrawl.parse import extract_links
from thilicrawl.testsite import SiteConfig, SiteProcess
from thilicrawl.testsite.site import big_page_html


async def heartbeat(stop: asyncio.Event, lags: list[float]) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(0.01)
        lags.append(time.perf_counter() - started - 0.01)


async def measure(page: str, mode: str, pool: ProcessPoolExecutor) -> tuple[float, float]:
    stop, lags = asyncio.Event(), []
    beat = asyncio.create_task(heartbeat(stop, lags))
    await asyncio.sleep(0.1)
    started = time.perf_counter()
    if mode == "inline":
        extract_links(page, "http://x/")
    elif mode == "thread":
        await asyncio.to_thread(extract_links, page, "http://x/")
    else:
        await asyncio.get_running_loop().run_in_executor(pool, extract_links, page, "http://x/")
    took = time.perf_counter() - started
    stop.set()
    await beat
    return took, max(lags)


def main() -> None:
    page = big_page_html(150_000, 2000, "http://127.0.0.1:1").decode()
    print(f"Part 1: parse one {len(page) / 1e6:.1f} MB page; how long is the event loop kept waiting?\n")
    with ProcessPoolExecutor(1) as pool:
        pool.submit(extract_links, "<a href='/'>x</a>", "http://x/").result()  # warm the worker
        for mode in ("inline", "thread", "process"):
            took, worst = asyncio.run(measure(page, mode, pool))
            print(f"  {mode:<8} parse took {took:5.2f}s | worst event-loop delay {worst * 1000:7.0f} ms")

    print(
        "\nPart 2: crawl a site whose first page is that big. The slowest page on this site answers in "
        "300 ms and the threshold is 1 s, so every flag is a false alarm.\n"
    )
    with SiteProcess(SiteConfig(pages=150, slow_ms=300, limit_window_s=1.0)) as site:
        for mode in ("inline", "thread", "process"):
            config = CrawlConfig(
                timeout=1.0,
                deadline=4.0,
                parse_mode=mode,  # type: ignore[arg-type]
                inline_parse_limit=100_000,
            )
            report = asyncio.run(check_links(site.main_url + "/", config))
            flagged = [r for r in report.results.values() if r.slow]
            print(f"  {mode:<8} crawl {report.elapsed:5.1f}s | links flagged slow: {len(flagged)}")


if __name__ == "__main__":
    main()
