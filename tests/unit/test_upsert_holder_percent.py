"""Unit tests: 大戶/散戶持股佔比寫入 stock_holder_percent。

docs/refactor-plan.md T8。原覆蓋率 5%。

重點是 ON CONFLICT 的三欄語意不一致，且那是刻意的：
  name         → COALESCE（不覆寫既有股名）
  major_ratio  → EXCLUDED（直接覆寫，這是本次要更新的主資料）
  retail_ratio → COALESCE（偶發解析失敗時不可把歷史散戶比例蓋成 NULL）
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import FakeCursor, install_fake_pool
from tw_stock_rawdata import db_utils

URL = "postgres://x"
DATE = dt.date(2026, 8, 21)


def _run(monkeypatch, rows):
    cur = FakeCursor()
    conn = install_fake_pool(monkeypatch, db_utils, cur)
    n = db_utils.upsert_holder_percent(URL, DATE, rows)
    return n, cur, conn


class TestSkipConditions:
    def test_empty_rows_skips_db_entirely(self, monkeypatch) -> None:
        n, cur, conn = _run(monkeypatch, [])
        assert n == 0
        assert cur.executed_many == []
        assert not conn.committed

    def test_all_blank_symbols_skips_db(self, monkeypatch) -> None:
        n, cur, conn = _run(monkeypatch, [("", "a", 0.5, 0.1), ("   ", "b", 0.5, 0.1)])
        assert n == 0
        assert cur.executed_many == []
        assert not conn.committed

    def test_blank_symbol_rows_are_filtered_out(self, monkeypatch) -> None:
        n, cur, _ = _run(monkeypatch, [("2330", "台積電", 0.5, 0.1), ("", "x", 0.5, 0.1)])
        assert n == 1
        _sql, params = cur.executed_many[0]
        assert len(params) == 1
        assert params[0][0] == "2330"


class TestNormalization:
    def test_symbol_is_stripped(self, monkeypatch) -> None:
        _n, cur, _ = _run(monkeypatch, [(" 2330 ", "台積電", 0.5, 0.1)])
        assert cur.executed_many[0][1][0][0] == "2330"

    def test_blank_name_becomes_none(self, monkeypatch) -> None:
        """空白股名轉 None，才能讓 COALESCE 保留 DB 既有股名。"""
        _n, cur, _ = _run(monkeypatch, [("2330", "   ", 0.5, 0.1)])
        assert cur.executed_many[0][1][0][2] is None

    def test_none_name_stays_none(self, monkeypatch) -> None:
        _n, cur, _ = _run(monkeypatch, [("2330", None, 0.5, 0.1)])
        assert cur.executed_many[0][1][0][2] is None

    def test_trade_date_is_second_param(self, monkeypatch) -> None:
        _n, cur, _ = _run(monkeypatch, [("2330", "台積電", 0.5, 0.1)])
        assert cur.executed_many[0][1][0][1] == DATE

    def test_none_ratios_pass_through(self, monkeypatch) -> None:
        _n, cur, _ = _run(monkeypatch, [("2330", "台積電", None, None)])
        params = cur.executed_many[0][1][0]
        assert params[3] is None and params[4] is None


class TestConflictSemantics:
    def test_name_and_retail_use_coalesce_but_major_does_not(self, monkeypatch) -> None:
        """三欄語意刻意不同——major_ratio 直接覆寫，另兩欄不可覆寫成 NULL。"""
        _n, cur, _ = _run(monkeypatch, [("2330", "台積電", 0.5, 0.1)])
        sql, _params = cur.executed_many[0]
        assert "name = COALESCE(EXCLUDED.name" in sql
        assert "major_ratio = EXCLUDED.major_ratio" in sql
        assert "retail_ratio = COALESCE(EXCLUDED.retail_ratio" in sql

    def test_conflict_target_is_symbol_and_date(self, monkeypatch) -> None:
        _n, cur, _ = _run(monkeypatch, [("2330", "台積電", 0.5, 0.1)])
        sql, _ = cur.executed_many[0]
        assert "ON CONFLICT (symbol, trade_date)" in sql

    def test_commits_after_write(self, monkeypatch) -> None:
        _n, _cur, conn = _run(monkeypatch, [("2330", "台積電", 0.5, 0.1)])
        assert conn.committed
