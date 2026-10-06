"""A human-readable report (lesson 10): what was found, grouped by what a person should do about it."""

from __future__ import annotations

from collections.abc import Iterable

from .models import CrawlReport, LinkResult, Outcome


def _found_on(result: LinkResult) -> str:
    if not result.referrers:
        return "(the start URL)"
    extra = f" (+{len(result.referrers) - 1} more)" if len(result.referrers) > 1 else ""
    return f"found on {result.referrers[0]}{extra}"


def _section(title: str, rows: Iterable[str], total: int, limit: int) -> list[str]:
    shown = list(rows)[:limit]
    if total == 0:
        return []
    lines = ["", f"{title} ({total})"]
    lines += [f"  {row}" for row in shown]
    if total > len(shown):
        lines.append(f"  ... and {total - len(shown)} more")
    return lines


def render(report: CrawlReport, limit: int = 20) -> str:
    counts = report.count()
    lines = [f"Checked {len(report.results):,} links in {report.elapsed:.1f}s ({report.requests:,} requests)"]
    if report.interrupted:
        lines.append("INTERRUPTED: these are the results so far, not the whole site")
    lines.append("  " + "   ".join(f"{o.value} {counts.get(o, 0):,}" for o in Outcome if counts.get(o, 0)))

    broken = report.of(Outcome.BROKEN)
    lines += _section("BROKEN", (f"{r.status}  {r.url}   {_found_on(r)}" for r in broken), len(broken), limit)
    loops = report.of(Outcome.LOOP)
    lines += _section("REDIRECT LOOPS", (f"{r.url}   {r.error}" for r in loops), len(loops), limit)
    unreachable = report.of(Outcome.TIMEOUT) + report.of(Outcome.ERROR)
    lines += _section(
        "UNREACHABLE",
        (f"{r.outcome.value:<7} {r.url}   {r.error}" for r in unreachable),
        len(unreachable),
        limit,
    )
    slow = report.slowest(limit)
    total_slow = sum(1 for r in report.results.values() if r.slow)
    lines += _section("SLOW", (f"{r.latency:5.2f}s  {r.url}" for r in slow), total_slow, limit)
    chains = sorted((r for r in report.of(Outcome.REDIRECT) if r.hops >= 2), key=lambda r: -r.hops)
    lines += _section(
        "LONG REDIRECT CHAINS",
        (f"{r.hops} hops  {r.url}  ->  {r.final_url}" for r in chains),
        len(chains),
        limit,
    )
    skipped = report.of(Outcome.SKIPPED)
    lines += _section("SKIPPED (robots.txt)", (r.url for r in skipped), len(skipped), limit)
    if not report.problems:
        lines += ["", "No broken links."]
    return "\n".join(lines)
