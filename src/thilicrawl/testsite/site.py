"""The test site's content: a seeded page graph with planted faults, and a manifest of ground truth.

Real sites will not hang, rate-limit or loop *on demand*, and pointing an experimental crawler at a
stranger's server is how you accidentally attack it. So this drill crawls a **local site that is
generated from a seed**: same seed, same site. Every planted fault is recorded in a manifest, so a test
can say "the checker found the 40 broken links the generator planted" rather than "it returned what it
returned yesterday".

What is planted (counts are in ``SiteConfig``):

* a **tree of pages** — page ``i`` links to ``2i+1`` and ``2i+2`` — plus random cross links, so every page
  is reachable from ``/`` and the crawl is about a dozen levels deep;
* **broken links** (404 and 410), **redirect chains** of 1, 2 and 5 hops, and one **redirect loop**;
* **slow pages**, **flaky** pages (503 for the first few requests) and **rate-limited** pages (429 with
  ``Retry-After``);
* a second, **external** host with working, broken, **hung** (accepts and never answers) and slow pages,
  and one link to a port where **nothing is listening**;
* a **50 MB binary file** and a **multi-megabyte HTML page** with 150,000 links;
* a ``robots.txt`` that forbids ``/private/``, with pages under it that must *not* be fetched;
* links of every awkward kind: relative, absolute, with fragments, ``mailto:``, ``javascript:``.
"""

from __future__ import annotations

import posixpath
import random
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SiteConfig:
    pages: int = 2000
    seed: int = 7
    latency_ms: float = 8.0  # how long every ordinary request takes
    slow_ms: float = 1500.0  # how long the slow pages take
    capacity: int | None = None  # in-flight requests the server handles before answering 503 "overloaded"
    faults: bool = True  # False: a clean site of ordinary pages only
    flaky_failures: int = 2  # a flaky page fails this many times, then works
    limit_requests: int = 10  # a rate-limited page allows this many requests per window...
    limit_window_s: float = 2.0  # ...of this many seconds, then answers 429 + Retry-After
    big_blob_mb: int = 50
    big_page_links: int = 150_000
    broken_404: int = 30
    broken_410: int = 10
    redirects_1: int = 10
    redirects_2: int = 6
    redirects_5: int = 4
    slow: int = 10
    flaky: int = 5
    limited: int = 30
    external_ok: int = 40
    external_broken: int = 5
    external_hang: int = 3
    external_slow: int = 2
    private: int = 3


@dataclass
class Site:
    """Everything the server needs to answer, plus the ground truth."""

    config: SiteConfig
    main_base: str
    ext_base: str
    dead_url: str
    pages: dict[str, bytes] = field(default_factory=dict)  # path -> html
    gone: dict[str, int] = field(default_factory=dict)  # path -> 404 | 410
    redirects: dict[str, tuple[int, str]] = field(default_factory=dict)  # path -> (status, location)
    slow_paths: set[str] = field(default_factory=set)
    flaky_paths: set[str] = field(default_factory=set)
    limited_paths: set[str] = field(default_factory=set)
    private_paths: set[str] = field(default_factory=set)
    ext_ok: set[str] = field(default_factory=set)
    ext_gone: set[str] = field(default_factory=set)
    ext_hang: set[str] = field(default_factory=set)
    ext_slow: set[str] = field(default_factory=set)
    blob_path: str = "/files/blob.bin"
    big_page_path: str = "/big/links.html"
    robots_txt: str = "User-agent: *\nDisallow: /private/\n"
    manifest: dict[str, Any] = field(default_factory=dict)


_NONLINKS = ["mailto:hello@example.com", "javascript:void(0)", "tel:+15555550100", "#top"]


def _href(rng: random.Random, source: str, target_url: str, main_base: str) -> str:
    """Write a link to ``target_url`` as it might appear on page ``source``: in one of four styles."""
    if not target_url.startswith(main_base):
        return target_url  # external links are always absolute
    target = target_url[len(main_base) :]
    style = rng.randrange(4)
    if style == 0:
        return target
    if style == 1:
        return target_url
    if style == 2:
        return posixpath.relpath(target, posixpath.dirname(source) or "/")
    return target + "#section"


def _render(index: int, hrefs: list[str]) -> bytes:
    items = "\n".join(f'    <li><a href="{h}">link</a></li>' for h in hrefs)
    return (
        f"<!doctype html>\n<html>\n<head><title>Page {index}</title></head>\n"
        f"<body>\n  <h1>Page {index}</h1>\n"
        f"  <ul>\n{items}\n  </ul>\n</body>\n</html>\n"
    ).encode()


def build_site(config: SiteConfig, main_base: str, ext_base: str, dead_url: str) -> Site:
    rng = random.Random(config.seed)
    site = Site(config, main_base, ext_base, dead_url)
    n = config.pages

    def path_of(i: int) -> str:
        return "/" if i == 0 else f"/p/{i}"

    # Each page's outgoing links, as absolute URLs, before they are written in a random style.
    outgoing: dict[int, list[str]] = {i: [] for i in range(n)}
    for i in range(n):
        for child in (2 * i + 1, 2 * i + 2):
            if child < n:
                outgoing[i].append(main_base + path_of(child))
        for _ in range(3):
            target = rng.randrange(n)
            if target != i:
                outgoing[i].append(main_base + path_of(target))

    expected: dict[str, dict[str, Any]] = {}
    all_urls: set[str] = {main_base + "/"} | {main_base + path_of(i) for i in range(n)}

    def attach(url: str, expect: dict[str, Any], sources: int = 1) -> None:
        """Link ``url`` from ``sources`` random pages, and record what the checker should find."""
        for i in rng.sample(range(n), min(sources, n)):
            outgoing[i].append(url)
        expected[url] = expect
        all_urls.add(url)

    if config.faults:
        for k in range(config.broken_404 + config.broken_410):
            status = 404 if k < config.broken_404 else 410
            path = f"/gone/{k}"
            site.gone[path] = status
            attach(main_base + path, {"outcome": "broken", "status": status}, sources=rng.randint(1, 3))

        chain_id = 0
        for hops, count in (
            (1, config.redirects_1),
            (2, config.redirects_2),
            (5, config.redirects_5),
        ):
            for _ in range(count):
                target_page = rng.randrange(1, n)
                final = main_base + path_of(target_page)
                hop_paths = [f"/r/{chain_id}/{h}" for h in range(hops)]
                for h, hop in enumerate(hop_paths):
                    nxt = hop_paths[h + 1] if h + 1 < hops else path_of(target_page)
                    site.redirects[hop] = (301 if h % 2 == 0 else 302, nxt)
                attach(
                    main_base + hop_paths[0],
                    {"outcome": "redirect", "status": 200, "hops": hops, "final": final},
                    sources=rng.randint(1, 2),
                )
                chain_id += 1

        site.redirects["/loop/a"] = (302, "/loop/b")
        site.redirects["/loop/b"] = (302, "/loop/a")
        attach(main_base + "/loop/a", {"outcome": "loop"}, sources=2)

        for k in range(config.slow):
            site.slow_paths.add(f"/slow/{k}")
            attach(main_base + f"/slow/{k}", {"outcome": "ok", "status": 200, "slow": True})
        for k in range(config.flaky):
            site.flaky_paths.add(f"/flaky/{k}")
            attach(
                main_base + f"/flaky/{k}",
                {"outcome": "ok", "status": 200, "attempts": config.flaky_failures + 1},
            )
        for k in range(config.limited):
            site.limited_paths.add(f"/limited/{k}")
            attach(main_base + f"/limited/{k}", {"outcome": "ok", "status": 200}, sources=2)

        for k in range(config.external_ok):
            site.ext_ok.add(f"/x/{k}")
            attach(ext_base + f"/x/{k}", {"outcome": "ok", "status": 200})
        for k in range(config.external_broken):
            site.ext_gone.add(f"/x/gone/{k}")
            attach(ext_base + f"/x/gone/{k}", {"outcome": "broken", "status": 404})
        for k in range(config.external_hang):
            site.ext_hang.add(f"/x/hang/{k}")
            attach(ext_base + f"/x/hang/{k}", {"outcome": "timeout"})
        for k in range(config.external_slow):
            site.ext_slow.add(f"/x/slow/{k}")
            attach(ext_base + f"/x/slow/{k}", {"outcome": "ok", "status": 200, "slow": True})
        attach(dead_url, {"outcome": "error"})

        attach(main_base + site.blob_path, {"outcome": "ok", "status": 200, "kind": "blob"}, sources=3)
        outgoing[0].append(main_base + site.big_page_path)
        expected[main_base + site.big_page_path] = {
            "outcome": "ok",
            "status": 200,
            "kind": "big_page",
        }
        all_urls.add(main_base + site.big_page_path)

        for k in range(config.private):
            site.private_paths.add(f"/private/{k}")
            attach(main_base + f"/private/{k}", {"outcome": "skipped"})

    # Render every page, writing each link in a random style and sprinkling in non-links.
    nonlinks = 0
    for i in range(n):
        hrefs = [_href(rng, path_of(i), url, main_base) for url in outgoing[i]]
        if rng.random() < 0.15:
            hrefs.append(rng.choice(_NONLINKS))
            nonlinks += 1
        site.pages[path_of(i)] = _render(i, hrefs)
    for path in site.private_paths:
        site.pages[path] = _render(-1, [])
    for path in site.slow_paths | site.flaky_paths | site.limited_paths:
        site.pages[path] = _render(-1, [])

    site.manifest = {
        "seed": config.seed,
        "config": dict(config.__dict__),
        "start_url": main_base + "/",
        "main_base": main_base,
        "external_base": ext_base,
        "dead_url": dead_url,
        "pages": n,
        "expected": expected,  # url -> what a correct checker reports (ordinary pages are all "ok")
        "all_urls": sorted(all_urls),
        "non_links_planted": nonlinks,
        "disallowed": sorted(main_base + p for p in site.private_paths),
        "counts": {
            "broken": sum(1 for e in expected.values() if e["outcome"] == "broken"),
            "redirect": sum(1 for e in expected.values() if e["outcome"] == "redirect"),
            "loop": sum(1 for e in expected.values() if e["outcome"] == "loop"),
            "timeout": sum(1 for e in expected.values() if e["outcome"] == "timeout"),
            "error": sum(1 for e in expected.values() if e["outcome"] == "error"),
            "skipped": sum(1 for e in expected.values() if e["outcome"] == "skipped"),
            "slow": sum(1 for e in expected.values() if e.get("slow")),
        },
    }
    return site


def big_page_html(links: int, pages: int, main_base: str) -> bytes:
    """A huge HTML page: ``links`` anchors pointing at ordinary pages. Slow to parse, on purpose."""
    parts = ["<!doctype html><html><body>"]
    for k in range(links):
        parts.append(f'<a href="{main_base}/p/{1 + k % max(pages - 1, 1)}">x{k}</a>')
    parts.append("</body></html>")
    return "".join(parts).encode()
