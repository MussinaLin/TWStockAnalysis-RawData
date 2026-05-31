"""Unit tests for the _retry_on_transient decorator (no network)."""

from __future__ import annotations

import pytest
import requests

from tw_stock_rawdata import sources
from tw_stock_rawdata.sources import (
    RETRY_ATTEMPTS,
    DataUnavailableError,
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
