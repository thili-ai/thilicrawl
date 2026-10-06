from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from thilicrawl.retry import RETRY_STATUSES, RetryPolicy, parse_retry_after


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),
        (502, True),
        (503, True),
        (504, True),
        (404, False),
        (410, False),
        (500, False),
        (200, False),
    ],
)
def test_only_failures_that_can_change_are_retried(status: int, retryable: bool) -> None:
    assert RetryPolicy().is_retryable_status(status) is retryable


def test_a_404_is_never_in_the_retry_set() -> None:
    assert 404 not in RETRY_STATUSES


def test_backoff_never_exceeds_a_ceiling_that_doubles() -> None:
    policy = RetryPolicy(base_delay=0.2, max_delay=5.0)
    rng = random.Random(0)
    for attempt, ceiling in [(1, 0.2), (2, 0.4), (3, 0.8), (4, 1.6), (5, 3.2), (6, 5.0), (12, 5.0)]:
        waits = [policy.backoff(attempt, rng) for _ in range(300)]
        assert min(waits) >= 0 and max(waits) <= ceiling


def test_backoff_is_jittered_not_a_fixed_schedule() -> None:
    policy, rng = RetryPolicy(), random.Random(0)
    assert len({round(policy.backoff(3, rng), 6) for _ in range(50)}) > 40  # many different waits


def test_backoff_is_reproducible_with_a_seed() -> None:
    policy = RetryPolicy()
    assert [policy.backoff(2, random.Random(7)) for _ in range(3)] == [
        policy.backoff(2, random.Random(7)) for _ in range(3)
    ]


def test_retry_after_wins_when_it_is_longer_than_the_backoff() -> None:
    assert RetryPolicy().delay(1, retry_after=2.0, rng=random.Random(0)) == 2.0


def test_backoff_wins_when_retry_after_is_shorter() -> None:
    policy = RetryPolicy(base_delay=10.0, max_delay=10.0)
    assert policy.delay(1, retry_after=0.001, rng=random.Random(0)) > 0.001


def test_an_absurd_retry_after_is_capped() -> None:
    assert RetryPolicy(max_retry_after=30.0).delay(1, retry_after=86_400.0, rng=random.Random(0)) == 30.0


def test_no_retry_after_means_just_the_backoff() -> None:
    assert 0 <= RetryPolicy().delay(1, retry_after=None, rng=random.Random(0)) <= 0.2


@pytest.mark.parametrize(("header", "seconds"), [("2", 2.0), (" 30 ", 30.0), ("0", 0.0)])
def test_parse_retry_after_seconds(header: str, seconds: float) -> None:
    assert parse_retry_after(header) == seconds


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(header, now=now) == 90.0


def test_a_retry_after_date_in_the_past_means_no_wait() -> None:
    now = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
    assert parse_retry_after(format_datetime(now - timedelta(hours=1), usegmt=True), now=now) == 0.0


@pytest.mark.parametrize("header", [None, "", "soon", "-3", "2.5"])
def test_unreadable_retry_after_is_ignored(header: str | None) -> None:
    assert parse_retry_after(header) is None
