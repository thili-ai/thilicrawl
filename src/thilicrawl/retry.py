"""Retry policy (lesson 5): a retry is a decision, not a loop.

Three separate questions, each answered here as a small pure function so it can be tested without a
network, a clock or a sleep:

1. **Should this failure be retried at all?** A 404 will still be a 404 in a second. A 503, a 429 and a
   timeout might not be. (``is_retryable_status``)
2. **How long to wait?** Exponential backoff — each retry waits longer — with *jitter*, a random
   fraction, so a hundred clients that failed together do not all come back at the same instant and
   fail together again. (``RetryPolicy.backoff``)
3. **What did the server ask for?** A ``Retry-After`` header is the server telling you exactly how
   long. Honour it, within a sane cap. (``parse_retry_after``, ``RetryPolicy.delay``)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

RETRY_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4  # total tries, including the first
    base_delay: float = 0.2  # the wait before the second try, before jitter
    max_delay: float = 5.0  # backoff never grows past this
    max_retry_after: float = 30.0  # we will not obey a server that asks us to wait longer than this
    retry_statuses: frozenset[int] = RETRY_STATUSES

    def is_retryable_status(self, status: int) -> bool:
        return status in self.retry_statuses

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Seconds to wait after failed ``attempt`` (1 = the first try): "full jitter" backoff.

        The ceiling doubles each attempt (0.2, 0.4, 0.8, ... up to ``max_delay``); the wait is a random
        point between zero and that ceiling.
        """
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return rng.uniform(0, ceiling)

    def delay(self, attempt: int, retry_after: float | None, rng: random.Random) -> float:
        """How long to wait before the next try: the backoff, or the server's request if that is longer."""
        wait = self.backoff(attempt, rng)
        if retry_after is not None:
            wait = max(wait, min(retry_after, self.max_retry_after))
        return wait


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` is a number of seconds (``"2"``) or a date (``"Wed, 21 Oct 2026 07:28:00 GMT"``).

    Returns seconds to wait, or ``None`` if the header is absent or unreadable.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
