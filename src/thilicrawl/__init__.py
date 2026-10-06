"""thilicrawl — a small, typed, concurrent link checker, built lesson by lesson in Learn Fast."""

__version__ = "0.1.0"

from .crawl import Crawler, check_links, check_urls
from .limits import HostLimiter
from .models import CrawlConfig, CrawlReport, LinkResult, Outcome
from .parse import extract_links
from .retry import RetryPolicy, parse_retry_after

__all__ = [
    "CrawlConfig",
    "CrawlReport",
    "Crawler",
    "HostLimiter",
    "LinkResult",
    "Outcome",
    "RetryPolicy",
    "check_links",
    "check_urls",
    "extract_links",
    "parse_retry_after",
]
