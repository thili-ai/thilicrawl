from __future__ import annotations

from collections.abc import Iterator

import pytest

from thilicrawl.testsite import SiteConfig, SiteServer


@pytest.fixture(scope="module")
def site() -> Iterator[SiteServer]:
    """A 120-page site with every kind of planted fault, fast enough for unit tests."""
    config = SiteConfig(
        pages=120,
        latency_ms=1,
        slow_ms=400,
        broken_404=6,
        broken_410=2,
        redirects_1=3,
        redirects_2=2,
        redirects_5=2,
        slow=2,
        flaky=2,
        limited=0,
        external_ok=6,
        external_broken=2,
        external_hang=1,
        external_slow=1,
        private=2,
        big_page_links=500,
        big_blob_mb=2,
    )
    with SiteServer(config) as server:
        yield server
