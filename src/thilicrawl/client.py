"""Checking one URL (lessons 2, 4 and 5).

``LinkChecker.check`` takes a URL and answers "is this link good?" — surviving everything a network
does to you. Reading it top to bottom is the order the lessons build it:

1. **One request**, asynchronously (``_once``): ``httpx.AsyncClient``, streamed so a 50 MB file is
   never downloaded just to learn its status.
2. **A deadline on everything** (lesson 4): a per-attempt timeout *and* a total deadline for the URL,
   because a host that accepts a connection and never answers will otherwise hold a task forever.
3. **Retries, decided carefully** (lesson 5): only for failures that can change (503, 429, timeouts),
   with jittered backoff, honouring ``Retry-After``, and a cap — never for a 404.
4. **Redirects, followed by hand**, so the chain is recorded and a loop is detected rather than
   followed until something gives up.

Nothing here ever swallows ``asyncio.CancelledError``: when the caller says stop, it stops.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .limits import HostLimiter
from .models import CrawlConfig, LinkResult, Outcome
from .parse import is_html, origin
from .retry import parse_retry_after

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class Verdict(Enum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    UNAVAILABLE = "unavailable"  # robots.txt could not be read


@dataclass(slots=True)
class Checked:
    """A result, and — for pages we were asked to read — the page's text."""

    result: LinkResult
    body: str | None = None


@dataclass(slots=True)
class _Reply:
    status: int
    headers: httpx.Headers
    latency: float
    body: bytes | None


class _Transient(Exception):
    """A failure that might not happen again: a timeout or a connection error."""

    def __init__(self, kind: Outcome, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = 1  # filled in by ``_fetch``, which knows how many tries it made


class LinkChecker:
    def __init__(
        self,
        config: CrawlConfig,
        client: httpx.AsyncClient,
        limiter: HostLimiter,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = config
        self._client = client
        self._limiter = limiter
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._robots: dict[str, RobotFileParser | str] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self.requests = 0

    # -- one request ---------------------------------------------------------------------------

    async def _once(self, method: str, url: str, want_body: bool, any_type: bool = False) -> _Reply:
        host = urlsplit(url).netloc
        async with self._limiter.slot(host):
            self.requests += 1
            start = time.perf_counter()
            try:
                async with self._client.stream(
                    method, url, headers={"User-Agent": self._cfg.user_agent}
                ) as resp:
                    latency = time.perf_counter() - start  # time until the response *headers* arrived
                    body: bytes | None = None
                    readable = any_type or is_html(resp.headers.get("content-type"))
                    if want_body and resp.status_code == 200 and readable:
                        body = await self._read_capped(resp)
                    return _Reply(resp.status_code, resp.headers, latency, body)
            except httpx.TimeoutException as exc:
                raise _Transient(
                    Outcome.TIMEOUT, f"{type(exc).__name__}: no answer in {self._cfg.timeout:g}s"
                ) from None
            except httpx.TransportError as exc:
                raise _Transient(Outcome.ERROR, f"{type(exc).__name__}: {exc}") from None

    async def _read_capped(self, resp: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= self._cfg.max_body_bytes:
                break  # a page this big is not worth reading further; we stop and keep what we have
        return b"".join(chunks)[: self._cfg.max_body_bytes]

    # -- retries ---------------------------------------------------------------------------------

    async def _fetch(
        self, method: str, url: str, want_body: bool, any_type: bool = False
    ) -> tuple[_Reply, int]:
        """One logical request, retried if it fails in a way that can change. Returns (reply, attempts)."""
        policy = self._cfg.retry
        for attempt in range(1, policy.max_attempts + 1):
            retry_after: float | None = None
            try:
                reply = await self._once(method, url, want_body, any_type)
                if method == "HEAD" and reply.status in (
                    405,
                    501,
                ):  # a server that dislikes HEAD: ask properly
                    reply = await self._once("GET", url, want_body, any_type)
                if not policy.is_retryable_status(reply.status) or attempt == policy.max_attempts:
                    return reply, attempt
                retry_after = parse_retry_after(reply.headers.get("retry-after"))
            except _Transient as exc:
                if attempt == policy.max_attempts:
                    exc.attempts = attempt
                    raise
            await self._sleep(policy.delay(attempt, retry_after, self._rng))  # no slot is held while we wait
        raise AssertionError("unreachable")  # pragma: no cover

    # -- one URL, with redirects and a deadline ------------------------------------------------

    async def check(
        self,
        url: str,
        *,
        method: str = "GET",
        want_body: bool = False,
        referrers: tuple[str, ...] = (),
    ) -> Checked:
        started = time.perf_counter()
        chain: list[str] = []
        latency = 0.0
        attempts = 1
        try:
            async with asyncio.timeout(self._cfg.deadline):  # the whole URL, however many attempts and hops
                current, seen = url, {url}
                while True:
                    reply, attempts = await self._fetch(method, current, want_body)
                    latency = max(latency, reply.latency)
                    location = reply.headers.get("location")
                    if reply.status in REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        chain.append(current)
                        if current in seen or len(chain) > self._cfg.max_redirects:
                            return self._done(
                                Outcome.LOOP,
                                url,
                                started,
                                latency,
                                attempts,
                                chain,
                                referrers,
                                error=f"redirect loop after {len(chain)} hops",
                            )
                        seen.add(current)
                        continue
                    break
        except _Transient as exc:
            return self._done(exc.kind, url, started, latency, exc.attempts, chain, referrers, error=str(exc))
        except (httpx.InvalidURL, ValueError) as exc:  # a link that is not a usable URL at all
            return self._done(
                Outcome.ERROR,
                url,
                started,
                latency,
                attempts,
                chain,
                referrers,
                error=f"invalid URL: {exc}",
            )
        except TimeoutError:
            return self._done(
                Outcome.TIMEOUT,
                url,
                started,
                latency,
                attempts,
                chain,
                referrers,
                error=f"gave up after the {self._cfg.deadline:g}s deadline",
            )

        if reply.status >= 400 or reply.status in REDIRECT_STATUSES:
            outcome = Outcome.BROKEN
        else:
            outcome = Outcome.REDIRECT if chain else Outcome.OK
        body = reply.body.decode("utf-8", errors="replace") if reply.body is not None else None
        done = self._done(
            outcome,
            url,
            started,
            latency,
            attempts,
            chain,
            referrers,
            status=reply.status,
            final_url=current,
        )
        return Checked(done.result, body)

    def _done(
        self,
        outcome: Outcome,
        url: str,
        started: float,
        latency: float,
        attempts: int,
        chain: list[str],
        referrers: tuple[str, ...],
        *,
        status: int | None = None,
        final_url: str | None = None,
        error: str | None = None,
    ) -> Checked:
        return Checked(
            LinkResult(
                url=url,
                outcome=outcome,
                status=status,
                final_url=final_url or (chain[-1] if chain else url),
                redirects=tuple(chain),
                latency=latency,
                elapsed=time.perf_counter() - started,
                attempts=attempts,
                error=error,
                referrers=referrers,
                slow=latency >= self._cfg.slow_threshold,
            )
        )

    # -- robots.txt -------------------------------------------------------------------------------

    async def check_robots(self, url: str) -> tuple[Verdict, str | None]:
        """May we fetch this URL? Reads each site's ``robots.txt`` once, then answers from memory.

        Per RFC 9309: a missing file (4xx) means "everything allowed"; one we cannot read (5xx, a timeout,
        a refused connection) means "assume nothing is" — the polite default for a crawler that cannot
        check. That last case is reported as ``UNAVAILABLE`` with the reason, *not* as "disallowed": a
        site that cannot be reached is a different finding from a site that said no.
        """
        site = origin(url)
        if site not in self._robots_locks:
            self._robots_locks[site] = asyncio.Lock()
        async with self._robots_locks[site]:  # single flight: concurrent callers wait for one fetch
            if site not in self._robots:
                self._robots[site] = await self._load_robots(site)
        found = self._robots[site]
        if isinstance(found, str):
            return Verdict.UNAVAILABLE, found
        allowed = found.can_fetch(self._cfg.user_agent, url)
        return (Verdict.ALLOWED if allowed else Verdict.DISALLOWED), None

    async def _load_robots(self, site: str) -> RobotFileParser | str:
        parser = RobotFileParser()
        try:
            async with asyncio.timeout(self._cfg.deadline):
                reply, _ = await self._fetch("GET", f"{site}/robots.txt", want_body=True, any_type=True)
        except _Transient as exc:
            return str(exc)
        except TimeoutError:
            return f"no answer within the {self._cfg.deadline:g}s deadline"
        if reply.status < 400:
            parser.parse((reply.body or b"").decode("utf-8", errors="replace").splitlines())
        elif reply.status < 500:
            parser.parse([])  # no robots.txt: everything is allowed
        else:
            return f"robots.txt answered {reply.status}"
        return parser
