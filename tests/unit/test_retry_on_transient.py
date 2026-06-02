"""Unit tests for the _retry_on_transient decorator (no network)."""

from __future__ import annotations

import datetime as dt

import pytest
import requests

from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import (
    PER_SYMBOL_RETRY_ATTEMPTS,
    PER_SYMBOL_RETRY_MAX_DELAY,
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_JITTER,
    RETRY_MAX_DELAY,
    DataUnavailableError,
    _retry_backoff_delay,
    _retry_on_transient,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Avoid real backoff delays in tests."""
    monkeypatch.setattr(sources.time, "sleep", lambda *_: None)


def test_returns_immediately_on_success():
    calls = {"n": 0}

    @_retry_on_transient
    def ok():
        calls["n"] += 1
        return "value"

    assert ok() == "value"
    assert calls["n"] == 1


def test_retries_then_succeeds_on_transient_valueerror():
    calls = {"n": 0}

    @_retry_on_transient
    def flaky():
        calls["n"] += 1
        if calls["n"] < RETRY_ATTEMPTS:  # fail until the last attempt
            raise ValueError("16 columns passed, passed data had 19 columns")
        return "recovered"

    assert flaky() == "recovered"
    assert calls["n"] == RETRY_ATTEMPTS


def test_persistent_valueerror_becomes_data_unavailable():
    calls = {"n": 0}

    @_retry_on_transient
    def always_bad():
        calls["n"] += 1
        raise ValueError("column mismatch")

    with pytest.raises(DataUnavailableError):
        always_bad()
    assert calls["n"] == RETRY_ATTEMPTS  # tried exactly RETRY_ATTEMPTS times


def test_request_exception_is_retried_then_reraised_as_is():
    """Network errors are retried, but the original RequestException is re-raised
    (NOT converted) so callers' network-error handling is preserved."""
    calls = {"n": 0}

    @_retry_on_transient
    def network_down():
        calls["n"] += 1
        raise requests.ConnectionError("boom")

    with pytest.raises(requests.ConnectionError):
        network_down()
    assert calls["n"] == RETRY_ATTEMPTS


def test_data_unavailable_is_not_retried():
    calls = {"n": 0}

    @_retry_on_transient
    def no_data():
        calls["n"] += 1
        raise DataUnavailableError("休市")

    with pytest.raises(DataUnavailableError):
        no_data()
    assert calls["n"] == 1  # raised immediately, no retry


def test_backoff_delay_never_shorter_than_deterministic():
    """Additive jitter only ever lengthens the wait: every delay is within
    [deterministic, deterministic + RETRY_JITTER]. This guarantees no profile's existing
    retry window is shortened (the per-symbol regression Codex flagged)."""
    for attempt in range(RETRY_ATTEMPTS):
        deterministic = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
        # Sample repeatedly to exercise the random jitter range.
        for _ in range(50):
            delay = _retry_backoff_delay(attempt)
            assert deterministic <= delay <= deterministic + RETRY_JITTER
            assert delay <= RETRY_MAX_DELAY + RETRY_JITTER


def test_backoff_window_is_long_enough_for_transient_5xx():
    """Worst-case total wait across all retries should comfortably exceed a 520's recovery window."""
    # Minimum guaranteed wait = sum of the deterministic backoff over the sleeps actually taken.
    min_total = sum(
        min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
        for attempt in range(RETRY_ATTEMPTS - 1)
    )
    # Guarantee the cumulative backoff covers tens-of-seconds 5xx/520 outages.
    assert min_total >= 30.0


def test_retry_sleeps_are_bounded_by_cap(monkeypatch):
    """The decorator's actual sleeps stay within the configured cap on a persistent failure."""
    slept: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda d: slept.append(d))

    @_retry_on_transient
    def always_bad():
        raise ValueError("column mismatch")

    with pytest.raises(DataUnavailableError):
        always_bad()

    assert len(slept) == RETRY_ATTEMPTS - 1  # no sleep after the final attempt
    assert all(0 < d <= RETRY_MAX_DELAY + RETRY_JITTER for d in slept)


def test_parametrized_per_symbol_profile_uses_short_window(monkeypatch):
    """Per-symbol profile retries fewer times with a smaller cap, so an outage isn't
    multiplied across hundreds of holdings."""
    slept: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda d: slept.append(d))
    calls = {"n": 0}

    @_retry_on_transient(
        attempts=PER_SYMBOL_RETRY_ATTEMPTS, max_delay=PER_SYMBOL_RETRY_MAX_DELAY
    )
    def always_bad():
        calls["n"] += 1
        raise ValueError("column mismatch")

    with pytest.raises(DataUnavailableError):
        always_bad()

    assert calls["n"] == PER_SYMBOL_RETRY_ATTEMPTS
    assert len(slept) == PER_SYMBOL_RETRY_ATTEMPTS - 1
    assert all(0 < d <= PER_SYMBOL_RETRY_MAX_DELAY + RETRY_JITTER for d in slept)
    # The per-symbol window must stay well under the long once-per-date window.
    assert sum(slept) <= RETRY_MAX_DELAY
    # ...but must not undershoot the original fixed [2,4] schedule (≥6s deterministic budget).
    deterministic_budget = sum(
        min(PER_SYMBOL_RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
        for attempt in range(PER_SYMBOL_RETRY_ATTEMPTS - 1)
    )
    assert sum(slept) >= deterministic_budget == 6.0


def test_fetch_twse_stock_day_uses_short_per_symbol_window(monkeypatch):
    """fetch_twse_stock_day is called once per holding, so it must use the short profile
    (≤ PER_SYMBOL_RETRY_ATTEMPTS attempts), not the long once-per-date window."""
    slept: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda d: slept.append(d))
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("endpoint down")

    monkeypatch.setattr(sources.requests.Session, "get", boom, raising=False)

    with pytest.raises(requests.ConnectionError):
        sources.fetch_twse_stock_day(requests.Session(), "2330", dt.date(2026, 6, 2))

    assert calls["n"] == PER_SYMBOL_RETRY_ATTEMPTS
    assert len(slept) == PER_SYMBOL_RETRY_ATTEMPTS - 1
