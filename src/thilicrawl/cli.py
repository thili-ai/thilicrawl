"""The command line (lesson 10).

    thilicrawl check http://localhost:8000/ --concurrency 20 --json

The same contract discipline as any tool people script against:

**Exit codes**
    0    every link is good
    1    the crawl finished and found problems (broken links, loops, unreachable hosts)
    2    bad usage: an unknown option, something that is not an http(s) URL
    3    the start URL itself could not be reached, so there was nothing to crawl
    70   a bug in this tool (an unexpected error inside the crawl)
    130  interrupted (Ctrl-C): the results so far are still printed

**stdout is the answer, stderr is the commentary.** The report (or ``--json``) goes to stdout.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer

from . import __version__
from .crawl import Crawler
from .models import CrawlConfig, Outcome, ParseMode
from .report import render
from .retry import RetryPolicy

EXIT_PROBLEMS = 1
EXIT_UNREACHABLE = 3
EXIT_BUG = 70
EXIT_INTERRUPTED = 130

app = typer.Typer(
    help="Check every link on a site, concurrently and politely.",
    epilog="Exit codes: 0 all good · 1 problems · 2 bad usage · 3 start URL unreachable · 130 interrupted",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"thilicrawl {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    """thilicrawl — a concurrent link checker."""


@app.command("check")
def check(
    url: Annotated[str, typer.Argument(help="The page to start from (http:// or https://).")],
    concurrency: Annotated[int, typer.Option(min=1, help="Most requests in flight at once, overall.")] = 20,
    per_host: Annotated[int, typer.Option(min=1, help="Most requests in flight to any one host.")] = 8,
    rate: Annotated[float | None, typer.Option(min=0.1, help="Requests per second to one host.")] = None,
    timeout: Annotated[float, typer.Option(min=0.1, help="Seconds to wait for one attempt.")] = 5.0,
    deadline: Annotated[
        float, typer.Option(min=0.1, help="Seconds to spend on one URL, all attempts included.")
    ] = 20.0,
    attempts: Annotated[int, typer.Option(min=1, help="Tries per request, including the first.")] = 4,
    max_pages: Annotated[
        int | None, typer.Option(min=1, help="Stop discovering new URLs after this many.")
    ] = None,
    depth: Annotated[
        int | None, typer.Option(min=0, help="How many clicks from the start page to follow.")
    ] = None,
    slow: Annotated[float, typer.Option(min=0.0, help="Flag responses slower than this many seconds.")] = 1.0,
    robots: Annotated[bool, typer.Option(help="Obey robots.txt on the start site.")] = True,
    external: Annotated[bool, typer.Option(help="Also check links that leave the site.")] = True,
    parse: Annotated[
        ParseMode, typer.Option(help="Where to parse pages: auto, inline, thread or process.")
    ] = "auto",
    limit: Annotated[int, typer.Option(min=1, help="Rows to show per section of the report.")] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the whole report as JSON.")] = False,
) -> None:
    """Crawl the site at URL and report every broken, looping, slow or unreachable link."""
    if not url.lower().startswith(("http://", "https://")):
        raise typer.BadParameter(f"{url!r} is not an http:// or https:// URL", param_hint="URL")
    config = CrawlConfig(
        concurrency=concurrency,
        per_host=per_host,
        rate_per_host=rate,
        timeout=timeout,
        deadline=deadline,
        retry=RetryPolicy(max_attempts=attempts),
        max_pages=max_pages,
        max_depth=depth,
        slow_threshold=slow,
        respect_robots=robots,
        check_external=external,
        parse_mode=parse,
    )
    crawler = Crawler([url], config)
    try:
        report = asyncio.run(crawler.run())
    except KeyboardInterrupt:
        report = crawler.report
        report.interrupted = True
        typer.echo("interrupted - printing what was checked so far", err=True)
    except ExceptionGroup as group:
        for error in group.exceptions:
            typer.echo(f"error: {type(error).__name__}: {error}", err=True)
        raise typer.Exit(EXIT_BUG) from None

    typer.echo(json.dumps(report.to_dict(), indent=2) if as_json else render(report, limit))

    start = report.results.get(url)
    if report.interrupted:
        raise typer.Exit(EXIT_INTERRUPTED)
    if start is not None and start.outcome in (Outcome.TIMEOUT, Outcome.ERROR):
        typer.echo(f"error: could not reach {url}: {start.error}", err=True)
        raise typer.Exit(EXIT_UNREACHABLE)
    if report.problems:
        raise typer.Exit(EXIT_PROBLEMS)
