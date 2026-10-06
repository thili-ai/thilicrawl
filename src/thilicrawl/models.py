"""The data model: what a check produced, how the crawler is configured, and the report."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .retry import RetryPolicy


class Outcome(StrEnum):
    OK = "ok"  # a 2xx, with no redirects on the way
    REDIRECT = "redirect"  # a 2xx, but only after one or more redirects
    BROKEN = "broken"  # a final 4xx or 5xx
    LOOP = "loop"  # redirects that never end
    TIMEOUT = "timeout"  # no answer in time: a hung host, or the per-URL deadline
    ERROR = "error"  # the connection itself failed (refused, reset, DNS)
    SKIPPED = "skipped"  # not fetched: robots.txt says no

    @property
    def is_problem(self) -> bool:
        return self in (Outcome.BROKEN, Outcome.LOOP, Outcome.TIMEOUT, Outcome.ERROR)


@dataclass(frozen=True, slots=True)
class LinkResult:
    url: str
    outcome: Outcome
    status: int | None = None  # the final HTTP status, if there was an answer
    final_url: str | None = None  # where redirects ended up
    redirects: tuple[str, ...] = ()  # every URL visited after the first, in order
    latency: float = 0.0  # seconds: the slowest single response we received
    elapsed: float = 0.0  # seconds: wall time for this URL, retries and backoff included
    attempts: int = 1  # requests made for the *final* hop (more than 1 means we retried)
    error: str | None = None
    referrers: tuple[str, ...] = ()  # pages that linked here (the first few)
    slow: bool = False

    @property
    def hops(self) -> int:
        return len(self.redirects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "outcome": self.outcome.value,
            "status": self.status,
            "final_url": self.final_url,
            "redirects": list(self.redirects),
            "latency": round(self.latency, 4),
            "elapsed": round(self.elapsed, 4),
            "attempts": self.attempts,
            "error": self.error,
            "referrers": list(self.referrers),
            "slow": self.slow,
        }


ParseMode = Literal["auto", "inline", "thread", "process"]


@dataclass(frozen=True)
class CrawlConfig:
    concurrency: int = 20  # worker tasks: the most requests in flight at once, overall
    per_host: int = 8  # ...and the most to any single host
    rate_per_host: float | None = None  # requests per second to one host (None = unlimited)
    timeout: float = 5.0  # seconds, per attempt
    deadline: float = 20.0  # seconds, for one URL in total: all attempts, redirects and backoff
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    max_redirects: int = 10
    slow_threshold: float = 1.0  # a response slower than this is flagged
    max_pages: int | None = None  # stop discovering new URLs after this many
    max_depth: int | None = None  # how many clicks from the start page to follow
    max_body_bytes: int = 10_000_000  # never read more than this of a page
    respect_robots: bool = True
    user_agent: str = "thilicrawl/0.1 (+https://github.com/thili-ai/thilicrawl)"
    check_external: bool = True  # also check links that leave the site (without crawling them)
    parse_mode: ParseMode = "auto"  # where link extraction runs: see Crawler._extract and lesson 9
    inline_parse_limit: int = 100_000  # "auto": bodies up to this many bytes are parsed on the event loop


@dataclass
class CrawlReport:
    start_url: str
    results: dict[str, LinkResult] = field(default_factory=dict)
    elapsed: float = 0.0
    requests: int = 0  # every HTTP request made: retries, redirect hops, robots.txt
    interrupted: bool = False

    def count(self) -> Counter[Outcome]:
        return Counter(r.outcome for r in self.results.values())

    def of(self, outcome: Outcome) -> list[LinkResult]:
        return sorted((r for r in self.results.values() if r.outcome is outcome), key=lambda r: r.url)

    @property
    def problems(self) -> list[LinkResult]:
        return sorted((r for r in self.results.values() if r.outcome.is_problem), key=lambda r: r.url)

    def slowest(self, n: int = 10) -> list[LinkResult]:
        slow = [r for r in self.results.values() if r.slow]
        return sorted(slow, key=lambda r: r.latency, reverse=True)[:n]

    def to_dict(self) -> dict[str, Any]:
        counts = self.count()
        return {
            "start_url": self.start_url,
            "elapsed": round(self.elapsed, 3),
            "requests": self.requests,
            "interrupted": self.interrupted,
            "checked": len(self.results),
            "counts": {o.value: counts.get(o, 0) for o in Outcome},
            "results": [r.to_dict() for r in sorted(self.results.values(), key=lambda r: r.url)],
        }
