from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from thilicrawl import CrawlConfig, Outcome, check_links, check_urls
from thilicrawl.crawl import Crawler
from thilicrawl.testsite import SiteConfig, SiteServer

from .helpers import fast_config, html

# -- against a pretend network ------------------------------------------------------------------------


def pages(site: Mapping[str, httpx.Response | str]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        found = site.get(request.url.path)
        if found is None:
            return httpx.Response(404)
        if isinstance(found, str):
            return html(*found.split())
        return found

    return handler


def cfg(**kw: object) -> CrawlConfig:
    kw.setdefault("respect_robots", False)
    return fast_config(**kw)


async def test_every_reachable_page_is_visited_exactly_once() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return html("/a", "/b") if request.url.path == "/" else html("/", "/a", "/b", "/c")

    report = await check_links("http://s.test/", cfg(), transport=httpx.MockTransport(handler))
    assert sorted(seen) == ["/", "/a", "/b", "/c"]
    assert len(report.results) == 4


async def test_broken_links_are_found_with_the_pages_that_link_to_them() -> None:
    site = {"/": "/ok /gone", "/ok": "/gone"}
    report = await check_links("http://s.test/", cfg(), transport=httpx.MockTransport(pages(site)))
    gone = report.results["http://s.test/gone"]
    assert gone.outcome is Outcome.BROKEN
    assert set(gone.referrers) == {"http://s.test/", "http://s.test/ok"}


async def test_external_links_are_checked_but_not_crawled() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "other.test":
            return html("/should-not-be-followed")
        return html("http://other.test/page")

    report = await check_links("http://s.test/", cfg(), transport=httpx.MockTransport(handler))
    assert "http://other.test/page" in report.results
    assert not any("should-not-be-followed" in u for u in seen)


async def test_external_links_use_head() -> None:
    methods: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        methods[request.url.host] = request.method
        return html("http://other.test/") if request.url.host == "s.test" else httpx.Response(200)

    await check_links("http://s.test/", cfg(), transport=httpx.MockTransport(handler))
    assert methods == {"s.test": "GET", "other.test": "HEAD"}


async def test_external_checking_can_be_switched_off() -> None:
    report = await check_links(
        "http://s.test/",
        cfg(check_external=False),
        transport=httpx.MockTransport(lambda r: html("http://other.test/")),
    )
    assert list(report.results) == ["http://s.test/"]


async def test_max_pages_stops_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return html(f"/{n + 1}", f"/{n + 2}")

    report = await check_links("http://s.test/", cfg(max_pages=7), transport=httpx.MockTransport(handler))
    assert len(report.results) == 7


async def test_max_depth_limits_how_far_we_follow() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.strip("/") or 0)
        return html(f"/{n + 1}")

    report = await check_links("http://s.test/", cfg(max_depth=2), transport=httpx.MockTransport(handler))
    assert len(report.results) == 3  # depth 0, 1 and 2: pages at depth 2 are checked but not read


async def test_an_infinite_site_still_ends_with_a_cap() -> None:
    report = await check_links(
        "http://s.test/", cfg(max_pages=50), transport=httpx.MockTransport(lambda r: html(f"{r.url.path}x"))
    )
    assert len(report.results) == 50


async def test_robots_disallowed_pages_are_never_fetched() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        return html("/private/x", "/open")

    report = await check_links(
        "http://s.test/", cfg(respect_robots=True), transport=httpx.MockTransport(handler)
    )
    assert "/private/x" not in seen
    assert report.results["http://s.test/private/x"].outcome is Outcome.SKIPPED
    assert not report.results["http://s.test/private/x"].outcome.is_problem


async def test_an_unreachable_start_url_is_an_error_not_a_clean_bill_of_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    report = await check_links(
        "http://s.test/", cfg(respect_robots=True), transport=httpx.MockTransport(handler)
    )
    start = report.results["http://s.test/"]
    assert start.outcome is Outcome.ERROR and "robots.txt" in (start.error or "")
    assert report.problems


async def test_a_redirect_inside_the_site_is_crawled_to_its_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return html("/old") if request.url.path == "/" else html("/deep")

    report = await check_links("http://s.test/", cfg(), transport=httpx.MockTransport(handler))
    assert report.results["http://s.test/old"].outcome is Outcome.REDIRECT
    assert "http://s.test/deep" in report.results  # found only by following the redirect


async def test_check_urls_checks_exactly_the_urls_given() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method + request.url.path)
        return httpx.Response(404 if request.url.path == "/b" else 200)

    report = await check_urls(
        ["http://s.test/a", "http://s.test/b", "http://t.test/c"],
        cfg(),
        transport=httpx.MockTransport(handler),
    )
    assert sorted(seen) == ["HEAD/a", "HEAD/b", "HEAD/c"]
    assert report.count()[Outcome.BROKEN] == 1


async def test_check_urls_with_nothing_to_check_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        await check_urls([])


async def test_the_sink_receives_every_result() -> None:
    got: list[str] = []

    async def sink(result: object) -> None:
        got.append(result.url)  # type: ignore[attr-defined]

    report = await check_links(
        "http://s.test/", cfg(), transport=httpx.MockTransport(pages({"/": "/a /b"})), sink=sink
    )
    assert sorted(got) == sorted(report.results)


async def test_a_slow_sink_slows_the_crawl_instead_of_piling_up() -> None:
    in_sink = 0
    peak = 0

    async def sink(result: object) -> None:
        nonlocal in_sink, peak
        in_sink += 1
        peak = max(peak, in_sink)
        await asyncio.sleep(0.005)
        in_sink -= 1

    await check_links(
        "http://s.test/",
        cfg(concurrency=3, per_host=3),
        transport=httpx.MockTransport(pages({"/": " ".join(f"/p{i}" for i in range(20))})),
        sink=sink,
    )
    assert peak <= 3  # never more results being delivered than there are workers


async def test_a_bug_in_a_worker_surfaces_instead_of_hanging() -> None:
    async def bad_sink(result: object) -> None:
        raise RuntimeError("sink exploded")

    with pytest.raises(ExceptionGroup) as info:
        await asyncio.wait_for(
            check_links(
                "http://s.test/", cfg(), transport=httpx.MockTransport(pages({"/": ""})), sink=bad_sink
            ),
            timeout=5,
        )
    assert isinstance(info.value.exceptions[0], RuntimeError)


async def test_cancelling_a_crawl_stops_it_and_keeps_partial_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/":
            await asyncio.sleep(60)
        return html("/a", "/b", "/c")

    crawler = Crawler(["http://s.test/"], cfg(), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(crawler.run())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "http://s.test/" in crawler.report.results  # what finished is still there
    assert crawler.report.elapsed > 0


@pytest.mark.parametrize("mode", ["inline", "thread", "process", "auto"])
async def test_every_parse_mode_finds_the_same_links(mode: str) -> None:
    big = "".join(f'<a href="/p{i}">x</a>' for i in range(300))
    handler = pages({"/": httpx.Response(200, text=big, headers={"content-type": "text/html"})})
    report = await check_links(
        "http://s.test/",
        cfg(parse_mode=mode, inline_parse_limit=100, max_depth=1),
        transport=httpx.MockTransport(handler),
    )
    assert len(report.results) == 301


# -- against the real test site: ground truth -------------------------------------------------------------


async def test_the_crawler_recovers_every_planted_fault(site: SiteServer) -> None:
    manifest = site.manifest
    report = await check_links(
        site.main_url + "/",
        CrawlConfig(
            concurrency=20,
            per_host=20,
            timeout=1.0,
            deadline=6.0,
            slow_threshold=0.3,
            retry=fast_config().retry,
        ),
    )
    expected = manifest["expected"]
    wrong: list[str] = []
    for url, want in expected.items():
        got = report.results.get(url)
        if got is None:
            wrong.append(f"{url}: never checked")
            continue
        if got.outcome != want["outcome"]:
            wrong.append(f"{url}: {got.outcome} != {want['outcome']}")
        elif "status" in want and got.status != want["status"]:
            wrong.append(f"{url}: status {got.status} != {want['status']}")
        elif "hops" in want and got.hops != want["hops"]:
            wrong.append(f"{url}: hops {got.hops} != {want['hops']}")
        elif want.get("slow") is not None and got.slow != want["slow"]:
            wrong.append(f"{url}: slow {got.slow} != {want['slow']}")
    assert wrong == []

    ordinary = [r for u, r in report.results.items() if u not in expected]
    assert ordinary and all(r.outcome is Outcome.OK and not r.slow for r in ordinary)
    assert set(report.results) == set(manifest["all_urls"]) - set()  # nothing missed, nothing invented


async def test_disallowed_pages_were_never_requested(site: SiteServer) -> None:
    before = site.stats.private_hits
    await check_links(site.main_url + "/", CrawlConfig(timeout=1.0, deadline=6.0, slow_threshold=0.3))
    assert site.stats.private_hits == before


async def test_a_flaky_page_is_retried_into_working() -> None:
    # Its own server: a flaky page only fails the first few times, so a shared one would already be "used up".
    config = SiteConfig(
        pages=20,
        latency_ms=1,
        flaky=3,
        broken_404=0,
        broken_410=0,
        redirects_1=0,
        redirects_2=0,
        redirects_5=0,
        slow=0,
        limited=0,
        external_ok=0,
        external_broken=0,
        external_hang=0,
        external_slow=0,
        private=0,
        big_page_links=50,
        big_blob_mb=1,
    )
    with SiteServer(config) as server:
        report = await check_links(
            server.main_url + "/", CrawlConfig(timeout=1.0, deadline=6.0, retry=fast_config().retry)
        )
    flaky = [r for u, r in report.results.items() if "/flaky/" in u]
    assert len(flaky) == 3 and all(r.outcome is Outcome.OK and r.attempts == 3 for r in flaky)
