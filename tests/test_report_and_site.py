from __future__ import annotations

import asyncio
import json

import httpx

from thilicrawl import CrawlReport, LinkResult, Outcome, check_links
from thilicrawl.report import render
from thilicrawl.testsite import SiteConfig, SiteProcess, SiteServer


def result(url: str, outcome: Outcome, **kw: object) -> LinkResult:
    return LinkResult(url=url, outcome=outcome, **kw)  # type: ignore[arg-type]


def test_a_clean_report_says_so() -> None:
    report = CrawlReport(
        "http://a.test/", {"http://a.test/": result("http://a.test/", Outcome.OK, status=200)}
    )
    assert "No broken links." in render(report)


def test_every_problem_gets_a_section_with_where_it_was_found() -> None:
    rs = [
        result(
            "http://a.test/x", Outcome.BROKEN, status=404, referrers=("http://a.test/", "http://a.test/b")
        ),
        result("http://a.test/l", Outcome.LOOP, error="redirect loop after 2 hops"),
        result("http://b.test/", Outcome.TIMEOUT, error="no answer"),
        result("http://a.test/s", Outcome.OK, status=200, latency=2.5, slow=True),
        result("http://a.test/r", Outcome.REDIRECT, status=200, redirects=("u1", "u2"), final_url="u2"),
        result("http://a.test/private/", Outcome.SKIPPED),
    ]
    text = render(CrawlReport("http://a.test/", {r.url: r for r in rs}, interrupted=True))
    for needle in (
        "BROKEN (1)",
        "found on http://a.test/ (+1 more)",
        "REDIRECT LOOPS",
        "UNREACHABLE",
        "SLOW",
        "LONG REDIRECT CHAINS",
        "SKIPPED",
        "INTERRUPTED",
    ):
        assert needle in text


def test_long_sections_are_truncated_with_a_count() -> None:
    rs = {f"http://a.test/{i}": result(f"http://a.test/{i}", Outcome.BROKEN, status=404) for i in range(30)}
    assert "... and 25 more" in render(CrawlReport("http://a.test/", rs), limit=5)


def test_the_report_serialises_to_json() -> None:
    report = CrawlReport(
        "http://a.test/", {"http://a.test/": result("http://a.test/", Outcome.OK, status=200)}
    )
    assert json.loads(json.dumps(report.to_dict()))["start_url"] == "http://a.test/"


# -- the test site itself behaves as advertised --------------------------------------------------------------


async def get(url: str, **kw: object) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5) as client:
        return await client.get(url, **kw)  # type: ignore[arg-type]


async def test_the_site_is_reproducible_from_its_seed() -> None:
    config = SiteConfig(pages=30, latency_ms=0, big_page_links=20, big_blob_mb=1)
    with SiteServer(config) as a, SiteServer(config) as b:
        assert (await get(a.main_url + "/p/7")).text.replace(a.main_url, "") == (
            await get(b.main_url + "/p/7")
        ).text.replace(b.main_url, "")
        assert len(a.manifest["all_urls"]) == len(b.manifest["all_urls"])


async def test_a_loaded_server_answers_503_when_over_capacity() -> None:
    config = SiteConfig(pages=10, latency_ms=200, capacity=3, big_page_links=10, big_blob_mb=1)
    with SiteServer(config) as server:
        replies = await asyncio.gather(*(get(server.main_url + "/p/1") for _ in range(12)))
    statuses = [r.status_code for r in replies]
    assert statuses.count(503) > 0 and statuses.count(200) > 0
    assert server.stats.overloaded == statuses.count(503)
    assert server.stats.max_inflight <= 3


async def test_a_rate_limited_page_answers_429_with_retry_after() -> None:
    config = SiteConfig(
        pages=10,
        latency_ms=0,
        limited=1,
        limit_requests=2,
        limit_window_s=5.0,
        big_page_links=10,
        big_blob_mb=1,
    )
    with SiteServer(config) as server:
        statuses = [(await get(server.main_url + "/limited/0")).status_code for _ in range(4)]
        limited = await get(server.main_url + "/limited/0")
    assert statuses[:2] == [200, 200] and 429 in statuses
    assert float(limited.headers["retry-after"]) > 0


async def test_a_hung_page_really_never_answers() -> None:
    config = SiteConfig(pages=10, latency_ms=0, external_hang=1, big_page_links=10, big_blob_mb=1)
    with SiteServer(config) as server:
        try:
            await get(server.ext_url + "/x/hang/0", timeout=0.3)
            raise AssertionError("it answered")
        except httpx.TimeoutException:
            pass


async def test_the_blob_is_streamed_and_big() -> None:
    config = SiteConfig(pages=10, latency_ms=0, big_page_links=10, big_blob_mb=2)
    with SiteServer(config) as server:
        async with (
            httpx.AsyncClient() as client,
            client.stream("GET", server.main_url + "/files/blob.bin") as r,
        ):
            assert r.headers["content-type"] == "application/octet-stream"
            total = sum([len(c) async for c in r.aiter_bytes()])
    assert total == 2_000_000


async def test_the_site_in_another_process_serves_and_reports_stats() -> None:
    config = SiteConfig(pages=20, latency_ms=0, big_page_links=10, big_blob_mb=1)
    site = SiteProcess(config).start()
    try:
        report = await check_links(site.main_url + "/")
        stats = site.stats()
        manifest = site.manifest
    finally:
        site.stop()
    assert stats["requests"] > 0 and manifest["pages"] == 20
    assert report.results[site.main_url + "/"].outcome is Outcome.OK
