# thilicrawl

A concurrent link checker: point it at a site and it tells you every broken, looping, slow or
unreachable link — checking hundreds of URLs at once without overwhelming anyone.

The reference implementation for the **Learn Fast** drill *Async Python: a Concurrent Link Checker*
(`lfpython-lfcrawl`). It is small enough to read in an afternoon and honest enough to learn from: the
numbers below were measured, and the design notes say where `asyncio` is the wrong tool.

```bash
pip install -e ".[dev]"          # Python 3.11 or newer
thilicrawl check https://example.com/ --concurrency 20
thilicrawl check http://localhost:8000/ --json > report.json
```

Exit codes: `0` all good · `1` problems found · `2` bad usage · `3` start URL unreachable ·
`70` a bug in the tool · `130` interrupted (the results so far are still printed).

As a library:

```python
import asyncio
from thilicrawl import CrawlConfig, check_links

report = asyncio.run(check_links("https://example.com/", CrawlConfig(concurrency=20, per_host=4)))
for result in report.problems:
    print(result.outcome, result.url, result.referrers)
```

## Map: lesson → file

| Lesson | Idea | Where |
|---|---|---|
| 1 | The sequential baseline, and why it is slow | `bench/concurrency.py` |
| 2 | One async request, streamed | `client.py` (`_once`) |
| 3 | A queue of URLs and a pool of workers | `crawl.py` (`Crawler`) |
| 4 | Deadlines on everything | `client.py` (`check`, `asyncio.timeout`) |
| 5 | Retries: which failures, how long to wait, `Retry-After` | `retry.py`, `client.py` (`_fetch`) |
| 6 | The frontier, and why it is *not* bounded | `crawl.py` |
| 7 | Politeness: per-host limits, rate, `robots.txt` | `limits.py`, `client.py` (`check_robots`) |
| 8 | Structured concurrency: `TaskGroup`, cancellation | `crawl.py` (`run`) |
| 9 | CPU-bound work on the event loop | `crawl.py` (`_extract`), `bench/parse_modes.py` |
| 10 | A CLI people can script, tests, the report | `cli.py`, `report.py`, `tests/` |

## The test site (the grader)

`thilicrawl.testsite` builds a website with **planted faults and a manifest of exactly what a correct
checker must report**: 404s and 410s, redirect chains of 1, 2 and 5 hops, a redirect loop, slow pages, flaky
pages (fail twice, then work), rate-limited pages (429 + `Retry-After`), a page that hangs forever, a
50 MB file, a 7 MB page with 150,000 links, `robots.txt`-disallowed pages, and links in four different
styles. It is generated from a seed, so a run is reproducible, and the tests assert the crawler recovers
the ground truth with zero mismatches. Run it by hand:

```bash
python -m thilicrawl.testsite --pages 2000      # prints the URLs and writes the manifest
```

## What was measured

All on one laptop, test site in a **separate process** (a thread-hosted server shares the interpreter
lock with the client and distorts timings — an early version of this benchmark did exactly that).
Re-run on your machine; the shape matters more than the numbers.

**300 URLs, ~50 ms each (`bench/concurrency.py`)**

| Approach | Time | URLs/s | Result |
|---|---|---|---|
| one at a time | 16.0 s | 19 | 300 ok |
| 16 threads | 1.2 s | 257 | 300 ok |
| `asyncio.gather`, unbounded | 0.65 s | 463 | **104 failed** (connection refused) |
| `asyncio` + `Semaphore(20)` | 0.86 s | 350 | 300 ok |
| `thilicrawl.check_urls` (20) | 0.99 s | 302 | 300 ok |

Unbounded is the fastest and the *wrong* one: it opens 300 connections at once and the server's listen
backlog drops some. Against a server that handles 40 requests at once and answers 503 beyond that, 600
URLs: unbounded gather got **43 ok / 557 failed**; `Semaphore(20)` got **599 ok / 1 failed** (3.3 s).

**One 6.9 MB page, 150,000 links (`bench/parse_modes.py`)**

| Parsed… | Parse time | Event loop kept waiting |
|---|---|---|
| inline | 1.26 s | **1258 ms** |
| in a thread | 1.27 s | 19 ms |
| in a process | 1.29 s | 2 ms |

Parsing is no faster anywhere. What changes is whether *everything else* freezes. Crawling a site
(slowest page: 300 ms; "slow" threshold: 1 s) with that page in it, inline parsing reported **6 healthy
links as slow**, a thread 2, a process 0 — the clock kept running while the loop was busy. `auto` mode
parses inline up to 100 KB and in a process above that.

**Failure modes (`bench/failure_modes.py`)**

- A host that accepts the connection and never answers: bounded at the deadline (3.0 s), not at "never".
- `429` + `Retry-After: 2`: waited the 2 seconds the server asked for, then succeeded.
- A page that fails twice then works: 3 attempts, 0.5 s of backoff, `ok`.
- Ctrl-C mid-crawl: exit 130, the 234 links checked so far are printed.

## Design notes

**Why a semaphore and not a thread pool?** Both bound concurrency; threads cost ~8 MB of stack each and
a context switch per request, a task costs a few KB. Threads are the right answer when the library you
must call is blocking and has no async version. Here `httpx` has one.

**Why is the frontier queue unbounded?** The workers both take from it and add to it. A bounded queue
that fills while every worker is blocked adding to it deadlocks the crawl. The real bounds are the
visited set, `max_pages`, and the worker count. Backpressure belongs on a *separate* consumer (`sink`).

**Why no slot is held while backing off.** A retry waits; a held slot would idle a concurrency token the
whole time. `_fetch` releases the slot between attempts.

**Where *not* to use `asyncio`.** CPU-bound work (lesson 9), a handful of requests (a script that makes
three calls does not need an event loop), code whose dependencies are blocking, and anything where the
simplest correct thing is a `for` loop.

**`robots.txt`, as RFC 9309 says.** Missing (4xx) → everything allowed. Unreadable (5xx, timeout, refused)
→ assume nothing is, and *say so*: an unreachable site is reported as unreachable (exit 3), never as
"no broken links".

## Development

```bash
uv sync --extra dev        # or: pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest --cov
```

CI runs the same gate on Python 3.11, 3.12 and 3.13. MIT licensed.

Built lesson by lesson in the Learn Fast drill *Async Python: a Concurrent Link Checker*, at [ask.thili.ai](https://ask.thili.ai).
