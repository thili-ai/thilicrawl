from __future__ import annotations

import asyncio

import httpx
import pytest

from thilicrawl.client import Verdict
from thilicrawl.models import Outcome

from .helpers import CountingStream, FakeTime, checker, fast_config, html


async def test_a_good_page_is_ok() -> None:
    async with checker(lambda r: httpx.Response(200, text="hi")) as c:
        result = (await c.check("http://a.test/")).result
    assert (result.outcome, result.status, result.attempts) == (Outcome.OK, 200, 1)


@pytest.mark.parametrize("status", [404, 410, 403])
async def test_client_errors_are_broken_and_never_retried(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    async with checker(handler) as c:
        result = (await c.check("http://a.test/x")).result
    assert (result.outcome, result.status, calls) == (Outcome.BROKEN, status, 1)


async def test_a_503_is_retried_until_it_works() -> None:
    answers = iter([503, 503, 200])
    fake = FakeTime()
    async with checker(lambda r: httpx.Response(next(answers)), fake=fake) as c:
        result = (await c.check("http://a.test/")).result
    assert (result.outcome, result.attempts) == (Outcome.OK, 3)
    assert len(fake.sleeps) == 2  # it waited between the attempts, on the fake clock


async def test_retries_stop_at_the_cap_and_report_the_last_status() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async with checker(handler) as c:
        result = (await c.check("http://a.test/")).result
    assert (result.outcome, result.status, result.attempts, calls) == (Outcome.BROKEN, 503, 4, 4)


async def test_retry_after_is_honoured() -> None:
    answers = iter([429, 200])
    fake = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(answers)
        return httpx.Response(status, headers={"Retry-After": "3"} if status == 429 else {})

    async with checker(handler, fake=fake) as c:
        result = (await c.check("http://a.test/")).result
    assert result.outcome is Outcome.OK
    assert fake.sleeps == [3.0]  # waited what the server asked, not our own (shorter) backoff


async def test_a_connection_error_is_retried_then_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with checker(handler) as c:
        result = (await c.check("http://a.test/")).result
    assert result.outcome is Outcome.ERROR
    assert result.attempts == 4
    assert "ConnectError" in (result.error or "")


async def test_a_timeout_is_reported_as_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async with checker(handler) as c:
        result = (await c.check("http://a.test/")).result
    assert result.outcome is Outcome.TIMEOUT


async def test_the_deadline_bounds_a_host_that_never_answers() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    async with checker(handler, fast_config(deadline=0.2, timeout=30.0)) as c:
        started = asyncio.get_running_loop().time()
        result = (await c.check("http://a.test/")).result
        took = asyncio.get_running_loop().time() - started
    assert result.outcome is Outcome.TIMEOUT
    assert took < 2
    assert "deadline" in (result.error or "")


async def test_a_redirect_chain_is_followed_and_recorded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        hops = {"/a": "/b", "/b": "/c"}
        if request.url.path in hops:
            return httpx.Response(301, headers={"Location": hops[request.url.path]})
        return httpx.Response(200)

    async with checker(handler) as c:
        result = (await c.check("http://a.test/a")).result
    assert result.outcome is Outcome.REDIRECT
    assert result.redirects == ("http://a.test/b", "http://a.test/c")
    assert result.final_url == "http://a.test/c"
    assert result.hops == 2


async def test_a_redirect_loop_is_detected_not_followed_forever() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        other = "/b" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"Location": other})

    async with checker(handler) as c:
        result = (await c.check("http://a.test/a")).result
    assert result.outcome is Outcome.LOOP
    assert calls <= 3


async def test_a_redirect_that_never_ends_is_cut_off() -> None:
    n = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal n
        n += 1
        return httpx.Response(302, headers={"Location": f"/next/{n}"})

    async with checker(handler, fast_config(max_redirects=5)) as c:
        result = (await c.check("http://a.test/")).result
    assert result.outcome is Outcome.LOOP
    assert n == 6


async def test_head_falls_back_to_get_when_the_server_dislikes_head() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(405 if request.method == "HEAD" else 200)

    async with checker(handler) as c:
        result = (await c.check("http://a.test/", method="HEAD")).result
    assert result.outcome is Outcome.OK
    assert methods == ["HEAD", "GET"]


async def test_the_body_is_returned_only_for_html_when_asked() -> None:
    async with checker(lambda r: html("/x")) as c:
        assert (await c.check("http://a.test/")).body is None
        assert "/x" in ((await c.check("http://a.test/", want_body=True)).body or "")


async def test_a_non_html_response_is_not_downloaded() -> None:
    stream = CountingStream(50_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, stream=stream)

    async with checker(handler) as c:
        checked = await c.check("http://a.test/big.bin", want_body=True)
    assert checked.result.outcome is Outcome.OK and checked.body is None
    assert stream.read < 1_000_000  # nowhere near 50 MB was pulled across


async def test_an_oversized_page_is_read_only_up_to_the_cap() -> None:
    stream = CountingStream(50_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    async with checker(handler, fast_config(max_body_bytes=100_000)) as c:
        checked = await c.check("http://a.test/", want_body=True)
    assert checked.body is not None and len(checked.body) <= 100_000
    assert stream.read < 1_000_000


async def test_slow_responses_are_flagged() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    async with checker(handler, fast_config(slow_threshold=0.01)) as c:
        assert (await c.check("http://a.test/")).result.slow


async def test_an_unusable_url_is_an_error_result_not_an_exception() -> None:
    async with checker(lambda r: httpx.Response(200)) as c:
        result = (await c.check("http://[not-an-address/")).result
    assert result.outcome is Outcome.ERROR


async def test_cancellation_is_not_swallowed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return httpx.Response(200)

    async with checker(handler) as c:
        task = asyncio.create_task(c.check("http://a.test/"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- robots.txt -----------------------------------------------------------------------------------------


def robots_handler(status: int, text: str = "", calls: list[str] | None = None):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        return httpx.Response(status, text=text)

    return handler


async def test_robots_disallow_is_obeyed() -> None:
    handler = robots_handler(200, "User-agent: *\nDisallow: /private/\n")
    async with checker(handler) as c:
        assert (await c.check_robots("http://a.test/private/x"))[0] is Verdict.DISALLOWED
        assert (await c.check_robots("http://a.test/public"))[0] is Verdict.ALLOWED


async def test_a_missing_robots_txt_allows_everything() -> None:
    async with checker(robots_handler(404)) as c:
        assert (await c.check_robots("http://a.test/anything"))[0] is Verdict.ALLOWED


async def test_an_unreadable_robots_txt_is_unavailable_with_a_reason() -> None:
    async with checker(robots_handler(503)) as c:
        verdict, why = await c.check_robots("http://a.test/x")
    assert verdict is Verdict.UNAVAILABLE and "503" in (why or "")


async def test_a_dead_host_is_unavailable_not_disallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with checker(handler) as c:
        assert (await c.check_robots("http://a.test/x"))[0] is Verdict.UNAVAILABLE


async def test_robots_is_fetched_once_even_when_asked_concurrently() -> None:
    calls: list[str] = []
    async with checker(robots_handler(200, "User-agent: *\nAllow: /\n", calls)) as c:
        await asyncio.gather(*(c.check_robots(f"http://a.test/{i}") for i in range(25)))
        await c.check_robots("http://a.test/again")
    assert calls == ["/robots.txt"]


async def test_robots_is_remembered_per_site() -> None:
    calls: list[str] = []
    async with checker(robots_handler(404, calls=calls)) as c:
        await c.check_robots("http://a.test/x")
        await c.check_robots("http://b.test/x")
    assert calls == ["/robots.txt", "/robots.txt"]
