"""Link extraction (lesson 9): the CPU-bound step.

Everything else in this package *waits* — on the network. This is the one step that *computes*:
turning an HTML page into the list of URLs it links to. For an ordinary page it is instant. For a
multi-megabyte page with 150,000 links it takes seconds of solid CPU — and an ``async`` program has a
single thread running every task, so while one task computes, **no other task runs at all**. Sockets
sit unserviced, timeouts cannot fire, every other request stalls.

``extract_links`` is deliberately written the plain, pure-Python way (the standard library's
``html.parser``) so that the stall is real and measurable. How the crawler keeps it off the event loop
is the lesson: see ``Crawler._extract``.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

LINK_SCHEMES = {"http", "https"}


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.hrefs.append(value)


def extract_links(html: str, base_url: str) -> list[str]:
    """Every ``http(s)`` link in ``html``, resolved against ``base_url``, without fragments, in order, once.

    ``mailto:``, ``javascript:``, ``tel:`` and bare ``#fragment`` links are not web pages and are dropped.
    """
    collector = _AnchorCollector()
    collector.feed(html)
    collector.close()
    seen: set[str] = set()
    links: list[str] = []
    for href in collector.hrefs:
        href = href.strip()
        if href.startswith("#"):
            continue  # a link to a spot on this same page is not a link to check
        try:
            absolute, _fragment = urldefrag(urljoin(base_url, href))
            scheme = urlsplit(absolute).scheme
        except ValueError:
            continue  # e.g. "http://[oops": a typo on someone's page must not stop the crawl
        if scheme not in LINK_SCHEMES or absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


def is_html(content_type: str | None) -> bool:
    return content_type is not None and content_type.split(";", 1)[0].strip().lower() in (
        "text/html",
        "application/xhtml+xml",
    )


def origin(url: str) -> str:
    """``scheme://host:port`` — what "the same site" means."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
