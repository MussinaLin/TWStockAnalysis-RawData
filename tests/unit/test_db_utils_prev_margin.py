"""Unit tests for find_consensus_prev_trade_date and update_prev_day_margin_batch."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils


def test_find_consensus_prev_trade_date_returns_when_agree(monkeypatch):
    prev = dt.date(2026, 5, 12)
    # _consensus_prev_trade_date queries stock_daily_raw, then market_daily
    cur = FakeCursor(fetch_results=[(prev,), (prev,)], rowcounts=[])
    install_fake_pool(monkeypatch, db_utils, cur)

    result = db_utils.find_consensus_prev_trade_date("postgres://x", dt.date(2026, 5, 13))

    assert result == prev
    assert len(cur.executed) == 2
    sql_raw, _ = cur.executed[0]
    sql_market, _ = cur.executed[1]
    assert "stock_daily_raw" in sql_raw
    assert "market_daily" in sql_market


def test_find_consensus_prev_trade_date_returns_none_when_disagree(monkeypatch):
    """Gap in stock_daily_raw must not silently target an earlier day."""
    cur = FakeCursor(
        fetch_results=[(dt.date(2026, 5, 10),), (dt.date(2026, 5, 12),)],
        rowcounts=[],
    )
    install_fake_pool(monkeypatch, db_utils, cur)

    result = db_utils.find_consensus_prev_trade_date("postgres://x", dt.date(2026, 5, 13))

    assert result is None


def test_find_consensus_prev_trade_date_returns_none_when_either_missing(monkeypatch):
    cur = FakeCursor(
        fetch_results=[(None,), (dt.date(2026, 5, 12),)],
        rowcounts=[],
    )
    install_fake_pool(monkeypatch, db_utils, cur)

    result = db_utils.find_consensus_prev_trade_date("postgres://x", dt.date(2026, 5, 13))

    assert result is None


def test_update_prev_day_margin_batch_empty_returns_zero(monkeypatch):
    cur = FakeCursor(fetch_results=[], rowcounts=[])
    conn = install_fake_pool(monkeypatch, db_utils, cur)

    result = db_utils.update_prev_day_margin_batch("postgres://x", [])

    assert result == 0
    # No DB call when updates is empty
    assert cur.executed == []
    assert not conn.committed


def test_update_prev_day_margin_batch_executes_and_sums_rowcount(monkeypatch):
    cur = FakeCursor(fetch_results=[], rowcounts=[1, 1, 0])
    conn = install_fake_pool(monkeypatch, db_utils, cur)

    prev = dt.date(2026, 5, 12)
    updates = [
        (
            "2330",
            prev,
            {
                "margin_buy": 100,
                "margin_sell": 50,
                "margin_balance": 1000,
                "margin_change": 50,
                "short_sell": 5,
                "short_buy": 2,
                "short_balance": 20,
                "short_change": 3,
                "short_margin_ratio": 0.02,
            },
        ),
        (
            "2317",
            prev,
            {
                "margin_buy": 200,
                "margin_sell": None,  # NULL passthrough
                "margin_balance": 2000,
                "margin_change": None,
                "short_sell": 0,
                "short_buy": 0,
                "short_balance": 0,
                "short_change": 0,
                "short_margin_ratio": None,
            },
        ),
        (
            "9999",
            prev,
            {
                "margin_buy": 1,
                "margin_sell": 1,
                "margin_balance": 1,
                "margin_change": 0,
                "short_sell": 0,
                "short_buy": 0,
                "short_balance": 0,
                "short_change": 0,
                "short_margin_ratio": 0.0,
            },
        ),
    ]

    result = db_utils.update_prev_day_margin_batch("postgres://x", updates)

    assert result == 2  # 1 + 1 + 0
    assert len(cur.executed) == 3
    assert conn.committed

    # Check first row's SQL & params (param order matches _PREV_MARGIN_COLS + symbol + date)
    sql, params = cur.executed[0]
    assert "UPDATE stock_daily_raw" in sql
    assert "margin_buy = %s" in sql
    assert "short_margin_ratio = %s" in sql
    assert "WHERE symbol = %s AND trade_date = %s" in sql
    assert params[-2] == "2330"
    assert params[-1] == prev
    # First value is margin_buy = 100
    assert params[0] == 100
    # Last numeric value before key is short_margin_ratio = 0.02
    assert params[-3] == 0.02


def test_update_prev_day_margin_batch_passes_none(monkeypatch):
    """None values should be passed through as NULL (not skipped)."""
    cur = FakeCursor(fetch_results=[], rowcounts=[1])
    install_fake_pool(monkeypatch, db_utils, cur)

    updates = [
        (
            "1234",
            dt.date(2026, 5, 12),
            {col: None for col in db_utils._PREV_MARGIN_COLS},
        )
    ]
    db_utils.update_prev_day_margin_batch("postgres://x", updates)

    sql, params = cur.executed[0]
    # First N params (margin columns) are all None
    n_cols = len(db_utils._PREV_MARGIN_COLS)
    assert all(p is None for p in params[:n_cols])
    assert params[-2] == "1234"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
