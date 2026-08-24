"""Unit tests: 大盤 D-1 融資餘額事後修正。

docs/refactor-plan.md T5。原覆蓋率 4%（71 行只有 1 行被執行），CC 8。
這是 db_utils 邏輯最繞的一段：要同時處理 D-1 與 D-2 兩層共識日推算，
且「找不到共識」與「值為 NULL」必須導向不同結果。

fetchone 的呼叫順序（測試用 FakeCursor 依序供給）：
  1-2. _consensus_prev_trade_date(current) → stock_daily_raw / market_daily 的 MAX
  3.   SELECT margin_balance, margin_balance_change FROM market_daily WHERE D-1
  4-5. _consensus_prev_trade_date(D-1) → 同上兩查，推 D-2
  6.   SELECT margin_balance FROM market_daily WHERE D-2
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils

URL = "postgres://x"
D = dt.date(2026, 8, 21)
D1 = dt.date(2026, 8, 20)
D2 = dt.date(2026, 8, 19)


def _run(monkeypatch, fetches, api_prev_balance=1000):
    cur = FakeCursor(fetch_results=fetches)
    conn = install_fake_pool(monkeypatch, db_utils, cur)
    result = db_utils.correct_prev_margin_balance(URL, D, api_prev_balance)
    return result, cur, conn


class TestAbortConditions:
    def test_no_d1_consensus_returns_none(self, monkeypatch) -> None:
        """兩表 MAX 不一致（gap）時放棄修正，避免寫到錯的歷史 row。"""
        result, cur, conn = _run(monkeypatch, [(D1,), (D2,)])
        assert result is None
        assert not conn.committed

    def test_d1_missing_in_one_table_returns_none(self, monkeypatch) -> None:
        result, cur, conn = _run(monkeypatch, [(None,), (D1,)])
        assert result is None
        assert not conn.committed

    def test_market_daily_row_missing_returns_none(self, monkeypatch) -> None:
        result, cur, conn = _run(monkeypatch, [(D1,), (D1,), None])
        assert result is None
        assert not conn.committed

    def test_no_change_needed_returns_none(self, monkeypatch) -> None:
        """balance 與 change 都已正確時不寫。"""
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (1000, 200), (D2,), (D2,), (800,)],
            api_prev_balance=1000,
        )
        assert result is None
        assert not conn.committed


class TestCorrection:
    def test_balance_and_change_updated(self, monkeypatch) -> None:
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (900, 100), (D2,), (D2,), (800,)],
            api_prev_balance=1000,
        )
        assert result == (D1, 900, 1000, 100, 200)
        assert conn.committed
        sql, params = cur.executed[-1]
        assert "UPDATE market_daily" in sql
        assert params == (1000, 200, D1)

    def test_stale_change_fixed_even_when_balance_already_correct(self, monkeypatch) -> None:
        """balance 已一致但 change 仍 stale（先前 D-2 缺料存成 NULL）也要修。"""
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (1000, None), (D2,), (D2,), (800,)],
            api_prev_balance=1000,
        )
        assert result == (D1, 1000, 1000, None, 200)
        assert conn.committed

    def test_no_d2_consensus_yields_null_change(self, monkeypatch) -> None:
        """D-2 無共識 → change 誠實寫 NULL，不推測。"""
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (900, 100), (D2,), (dt.date(2026, 8, 18),)],
            api_prev_balance=1000,
        )
        assert result == (D1, 900, 1000, 100, None)
        assert cur.executed[-1][1] == (1000, None, D1)

    def test_d2_balance_null_yields_null_change(self, monkeypatch) -> None:
        """D-2 有共識但該日 margin_balance 為 NULL → change 同樣寫 NULL。"""
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (900, 100), (D2,), (D2,), (None,)],
            api_prev_balance=1000,
        )
        assert result == (D1, 900, 1000, 100, None)

    def test_d2_row_missing_yields_null_change(self, monkeypatch) -> None:
        result, cur, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (900, 100), (D2,), (D2,), None],
            api_prev_balance=1000,
        )
        assert result == (D1, 900, 1000, 100, None)

    def test_old_balance_null_is_reported(self, monkeypatch) -> None:
        result, _, conn = _run(
            monkeypatch,
            [(D1,), (D1,), (None, None), (D2,), (D2,), (800,)],
            api_prev_balance=1000,
        )
        assert result == (D1, None, 1000, None, 200)
        assert conn.committed
